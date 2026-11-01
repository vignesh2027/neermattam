// Command neermattam-gateway is the public web service: pages, report
// upload, the WhatsApp webhook and the live map stream. Configuration comes
// from environment variables; see docs/GATEWAY.md.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"neermattam/gateway/internal/app"
	"neermattam/gateway/internal/config"
	"neermattam/gateway/internal/demo"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "neermattam-gateway:", err)
		os.Exit(1)
	}
}

func run() error {
	cfg, err := config.FromEnv()
	if err != nil {
		return err
	}
	var level slog.Level
	if err := level.UnmarshalText([]byte(cfg.LogLevel)); err != nil {
		level = slog.LevelInfo
	}
	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level}))
	slog.SetDefault(log)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	// Background work outlives the signal context by the drain period, so
	// in-flight WhatsApp replies can finish.
	base, cancelBase := context.WithCancel(context.Background())
	defer cancelBase()

	a, err := app.New(base, cfg, log, app.Options{})
	if err != nil {
		return err
	}
	a.Run(base)

	whatsapp := "enabled"
	if !cfg.WhatsApp.Enabled() {
		whatsapp = "disabled, missing " + strings.Join(cfg.WhatsApp.Missing(), ", ")
	}
	log.Info("starting neermattam-gateway",
		"addr", cfg.Addr, "engine", cfg.EngineURL, "perception", cfg.PerceptionURL,
		"demo", cfg.Demo, "whatsapp", whatsapp, "service_area", cfg.ServiceArea.String())

	if cfg.Demo && a.Replay != nil {
		// The simulated Cyclone Michaung replay goes to the engine's
		// separate replay map only; the live map stays real reports.
		seeder := &demo.Seeder{
			Engine: a.Replay, Origins: a.ReplayOrigins, Hub: a.ReplayHub, Log: log,
			Drip: cfg.DemoDrip, Reseed: cfg.DemoReseedEvery,
		}
		go seeder.Run(base, cfg.ServiceArea.String())
	}

	srv := &http.Server{
		Addr:              cfg.Addr,
		Handler:           a.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       cfg.ReplyDeadline, // a 40 MB video on a slow phone link
		IdleTimeout:       2 * time.Minute,
		MaxHeaderBytes:    32 << 10,
		ErrorLog:          slog.NewLogLogger(log.Handler(), slog.LevelWarn),
		BaseContext:       func(net.Listener) context.Context { return base },
	}
	srv.RegisterOnShutdown(a.Shutdown)

	errc := make(chan error, 1)
	go func() { errc <- srv.ListenAndServe() }()

	select {
	case err := <-errc:
		if !errors.Is(err, http.ErrServerClosed) {
			return err
		}
	case <-ctx.Done():
		log.Info("shutdown signal received, draining")
	}
	drain, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := srv.Shutdown(drain); err != nil {
		log.Warn("drain incomplete", "error", err.Error())
	}
	cancelBase()
	a.Shutdown()
	log.Info("stopped")
	return nil
}
