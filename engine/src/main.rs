//! `neermattam-engine` server binary. Configuration comes from environment
//! variables; see `config.rs` or `docs/ENGINE.md`.

use neermattam_engine::api::{router, AppState};
use neermattam_engine::config::Config;
use neermattam_engine::graph::RoadGraph;
use neermattam_engine::persist::{self, PersistError};
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;

/// How long to wait for open connections after a shutdown signal.
const DRAIN_TIMEOUT: Duration = Duration::from_secs(10);

fn init_logging(json: bool) {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let builder = tracing_subscriber::fmt().with_env_filter(filter);
    if json {
        builder
            .json()
            .flatten_event(true)
            .with_current_span(false)
            .init();
    } else {
        builder.init();
    }
}

async fn shutdown_signal() {
    let ctrl_c = async {
        if let Err(e) = tokio::signal::ctrl_c().await {
            error!(error = %e, "cannot listen for ctrl-c");
            std::future::pending::<()>().await;
        }
    };
    #[cfg(unix)]
    let terminate = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut s) => {
                s.recv().await;
            }
            Err(e) => {
                error!(error = %e, "cannot listen for SIGTERM");
                std::future::pending::<()>().await;
            }
        }
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();
    tokio::select! {
        () = ctrl_c => {}
        () = terminate => {}
    }
}

fn load_snapshot(state: &AppState) {
    let Some(path) = &state.config.snapshot_path else {
        info!("persistence disabled (ENGINE_SNAPSHOT_PATH is empty)");
        return;
    };
    let mut grid = state.grid.write();
    match persist::load(path, &mut grid) {
        Ok(n) => info!(cells = n, path = %path.display(), "snapshot loaded"),
        Err(e @ (PersistError::Resolution { .. } | PersistError::Invalid { .. })) => {
            // Keep the old file for inspection rather than overwrite it.
            let aside =
                path.with_extension(format!("rejected-{}.json", chrono::Utc::now().timestamp()));
            let moved = std::fs::rename(path, &aside);
            warn!(error = %e, moved_to = %aside.display(), moved_ok = moved.is_ok(), "snapshot not loaded; starting with an empty map");
        }
        Err(e) => warn!(error = %e, "snapshot not loaded; starting with an empty map"),
    }
    // The history file is loaded after the cell state, and a bad one never
    // costs the cell state: it is moved aside and each cell keeps only its
    // latest reading as history.
    let hpath = persist::history_path(path);
    match persist::load_history(&hpath, &mut grid) {
        Ok(n) => info!(points = n, path = %hpath.display(), "history loaded"),
        Err(e @ (PersistError::Resolution { .. } | PersistError::Invalid { .. })) => {
            let aside =
                hpath.with_extension(format!("rejected-{}.json", chrono::Utc::now().timestamp()));
            let moved = std::fs::rename(&hpath, &aside);
            warn!(error = %e, moved_to = %aside.display(), moved_ok = moved.is_ok(), "history not loaded; cell state kept");
        }
        Err(e) => warn!(error = %e, "history not loaded; cell state kept"),
    }
}

#[tokio::main]
async fn main() -> ExitCode {
    let config = match Config::from_env() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("neermattam-engine: {e}");
            return ExitCode::from(2);
        }
    };
    init_logging(config.log_json);
    info!(
        bind = %config.bind,
        resolution = u8::from(config.resolution),
        cities = %config.cities.iter().map(|c| c.id.as_str()).collect::<Vec<_>>().join(","),
        "starting neermattam-engine"
    );

    let state = AppState::new(config);
    load_snapshot(&state);
    if state.grid.read().history_points() > 0 {
        state.mark_history_dirty();
    }

    // Load every city's road graph in the background, in parallel; /readyz
    // and /v1/cities report progress. A city whose graph fails still accepts
    // readings; only its routes are unavailable.
    {
        let started = std::time::Instant::now();
        let mut loads = tokio::task::JoinSet::new();
        for city in state.config.cities.clone() {
            let st = Arc::clone(&state);
            loads.spawn_blocking(move || {
                let t = std::time::Instant::now();
                match RoadGraph::load_pbf(&city.osm_path, st.config.resolution) {
                    Ok(g) => {
                        let s = g.stats();
                        info!(
                            city = %city.id,
                            ways = s.ways,
                            nodes = s.nodes,
                            edges = s.edges,
                            segments = s.segments,
                            memory_mib = format!("{:.1}", s.memory_bytes as f64 / 1_048_576.0),
                            parse_ms = s.parse_ms,
                            build_ms = s.build_ms,
                            wall_ms = t.elapsed().as_millis() as u64,
                            "road graph ready"
                        );
                        st.set_graph(&city.id, g);
                        Some(s.memory_bytes)
                    }
                    Err(e) => {
                        error!(city = %city.id, error = %e, "road graph failed to load; routing in this city is unavailable");
                        st.set_graph_error(&city.id, e.to_string());
                        None
                    }
                }
            });
        }
        let st = Arc::clone(&state);
        tokio::spawn(async move {
            let (mut ok, mut failed, mut bytes) = (0, 0, 0usize);
            while let Some(r) = loads.join_next().await {
                match r {
                    Ok(Some(b)) => {
                        ok += 1;
                        bytes += b;
                    }
                    _ => failed += 1,
                }
            }
            if failed > 0 {
                let primary_ok = st.graph(&st.config.primary_city).is_some();
                error!(
                    failed,
                    primary_city = %st.config.primary_city,
                    primary_ok,
                    "ROAD GRAPHS FAILED TO LOAD: routes in those cities answer 503 until the extracts are fixed; /readyz lists them{}",
                    if primary_ok { "" } else { ", and stays 503 because the primary city is among them" }
                );
            }
            info!(
                loaded = ok,
                failed,
                memory_mib = format!("{:.1}", bytes as f64 / 1_048_576.0),
                wall_ms = started.elapsed().as_millis() as u64,
                "all road graphs processed"
            );
        });
    }

    let snapshots = tokio::spawn(Arc::clone(&state).snapshot_loop());

    let listener = match tokio::net::TcpListener::bind(state.config.bind).await {
        Ok(l) => l,
        Err(e) => {
            error!(error = %e, bind = %state.config.bind, "cannot bind");
            return ExitCode::from(1);
        }
    };
    info!(addr = %state.config.bind, "listening");
    if !state.config.bind_is_internal() {
        warn!(
            addr = %state.config.bind,
            "ENGINE_BIND is not a loopback or private address; the engine has no authentication and must only be reachable from the gateway"
        );
    }

    let shutdown_state = Arc::clone(&state);
    let (drain_tx, drain_rx) = tokio::sync::oneshot::channel::<()>();
    let server =
        axum::serve(listener, router(Arc::clone(&state))).with_graceful_shutdown(async move {
            shutdown_signal().await;
            info!("shutdown signal received, draining connections");
            shutdown_state.begin_shutdown();
            let _ = drain_tx.send(());
        });
    let drain_deadline = async {
        if drain_rx.await.is_ok() {
            tokio::time::sleep(DRAIN_TIMEOUT).await;
        } else {
            std::future::pending::<()>().await;
        }
    };
    tokio::select! {
        r = server => if let Err(e) = r { error!(error = %e, "server error") },
        () = drain_deadline => warn!("connections did not drain in time; closing"),
    }

    state.begin_shutdown();
    let _ = snapshots.await;
    // Cell state first: it is what routing needs after a restart.
    let st = Arc::clone(&state);
    match tokio::task::spawn_blocking(move || st.save_snapshot()).await {
        Ok(Ok(n)) => info!(cells = n, "final snapshot saved"),
        Ok(Err(e)) => error!(error = %e, "final snapshot failed"),
        Err(e) => error!(error = %e, "final snapshot task failed"),
    }
    match tokio::task::spawn_blocking(move || state.save_history()).await {
        Ok(Ok(n)) => info!(points = n, "final history saved"),
        Ok(Err(e)) => error!(error = %e, "final history snapshot failed"),
        Err(e) => error!(error = %e, "final history task failed"),
    }
    ExitCode::SUCCESS
}
