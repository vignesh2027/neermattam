// Command loadtest simulates people watching the live map: each viewer loads
// /map and its assets the way a browser does, holds the /api/stream event
// stream open, and revalidates /api/cells with its ETag now and then. It
// prints latencies and stream deliveries, and, given the gateway's pid,
// samples the process's resident memory and CPU while the test runs.
//
//	go run ./cmd/loadtest -n 50 -d 60s -pid $(pgrep -f neermattam-gateway)
//
// All viewers come from one address, so run the gateway with a raised
// API_RATE_PER_MIN and API_BURST, or the per-address limiter (correctly)
// refuses most of them.
package main

import (
	"bufio"
	"compress/gzip"
	"context"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type stats struct {
	mu   sync.Mutex
	lat  map[string][]time.Duration
	errs map[string]int
}

func (s *stats) add(kind string, d time.Duration, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err != nil {
		s.errs[kind+": "+err.Error()]++
		return
	}
	s.lat[kind] = append(s.lat[kind], d)
}

var assetRe = regexp.MustCompile(`(?:href|src)="(/static/[^"]+)"`)

func main() {
	base := flag.String("url", "http://127.0.0.1:8080", "gateway base URL")
	n := flag.Int("n", 50, "concurrent map viewers")
	dur := flag.Duration("d", 60*time.Second, "test duration")
	reval := flag.Duration("revalidate", 15*time.Second, "how often each viewer revalidates /api/cells")
	pid := flag.Int("pid", 0, "gateway process id to sample (optional)")
	flag.Parse()

	tr := &http.Transport{MaxIdleConnsPerHost: *n * 2, DisableCompression: true}
	client := &http.Client{Transport: tr}
	st := &stats{lat: map[string][]time.Duration{}, errs: map[string]int{}}
	var events, bytesIn atomic.Int64
	perViewer := make([]int64, *n)

	ctx, cancel := context.WithTimeout(context.Background(), *dur)
	defer cancel()

	var samples []sample
	var sampleWG sync.WaitGroup
	if *pid > 0 {
		sampleWG.Add(1)
		go func() {
			defer sampleWG.Done()
			samples = sampleProcess(ctx, *pid)
		}()
	}

	get := func(path, etag string) (int, string, []byte, time.Duration, error) {
		req, _ := http.NewRequestWithContext(ctx, "GET", *base+path, nil)
		req.Header.Set("Accept-Encoding", "gzip")
		if etag != "" {
			req.Header.Set("If-None-Match", etag)
		}
		t0 := time.Now()
		resp, err := client.Do(req)
		if err != nil {
			return 0, "", nil, 0, err
		}
		defer resp.Body.Close()
		raw, err := io.ReadAll(resp.Body)
		d := time.Since(t0)
		bytesIn.Add(int64(len(raw)))
		if err != nil {
			return 0, "", nil, d, err
		}
		body := raw
		if resp.Header.Get("Content-Encoding") == "gzip" {
			zr, err := gzip.NewReader(strings.NewReader(string(raw)))
			if err == nil {
				body, _ = io.ReadAll(zr)
			}
		}
		if resp.StatusCode >= 400 {
			return resp.StatusCode, "", body, d, fmt.Errorf("status %d", resp.StatusCode)
		}
		return resp.StatusCode, resp.Header.Get("ETag"), body, d, nil
	}

	start := time.Now()
	var wg sync.WaitGroup
	for i := 0; i < *n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			// Arrivals spread over the first two seconds.
			time.Sleep(time.Duration(i) * 2 * time.Second / time.Duration(*n))
			_, _, page, d, err := get("/map", "")
			st.add("GET /map", d, err)
			if err != nil {
				return
			}
			for _, m := range assetRe.FindAllStringSubmatch(string(page), -1) {
				_, _, _, d, err := get(m[1], "")
				st.add("GET /static (first visit)", d, err)
			}
			_, etag, _, d, err := get("/api/cells", "")
			st.add("GET /api/cells", d, err)

			go func() {
				req, _ := http.NewRequestWithContext(ctx, "GET", *base+"/api/stream", nil)
				resp, err := client.Do(req)
				if err != nil {
					if ctx.Err() == nil {
						st.add("stream", 0, err)
					}
					return
				}
				defer resp.Body.Close()
				if resp.StatusCode != 200 {
					st.add("stream", 0, fmt.Errorf("status %d", resp.StatusCode))
					return
				}
				sc := bufio.NewScanner(resp.Body)
				sc.Buffer(make([]byte, 64<<10), 1<<20)
				for sc.Scan() {
					line := sc.Text()
					bytesIn.Add(int64(len(line) + 1))
					if strings.HasPrefix(line, "event: cell") {
						events.Add(1)
						atomic.AddInt64(&perViewer[i], 1)
					}
				}
			}()

			t := time.NewTicker(*reval)
			defer t.Stop()
			for {
				select {
				case <-ctx.Done():
					return
				case <-t.C:
					code, e, _, d, err := get("/api/cells", etag)
					if ctx.Err() != nil {
						return
					}
					kind := "GET /api/cells revalidate (200)"
					if code == http.StatusNotModified {
						kind = "GET /api/cells revalidate (304)"
					}
					st.add(kind, d, err)
					if e != "" {
						etag = e
					}
				}
			}
		}(i)
	}
	wg.Wait()
	<-ctx.Done()
	sampleWG.Wait()
	elapsed := time.Since(start)

	fmt.Printf("viewers %d, duration %s\n\n", *n, elapsed.Round(time.Second))
	kinds := make([]string, 0, len(st.lat))
	for k := range st.lat {
		kinds = append(kinds, k)
	}
	sort.Strings(kinds)
	fmt.Printf("%-34s %6s %8s %8s %8s\n", "request", "count", "p50", "p95", "max")
	for _, k := range kinds {
		l := st.lat[k]
		sort.Slice(l, func(a, b int) bool { return l[a] < l[b] })
		fmt.Printf("%-34s %6d %8s %8s %8s\n", k, len(l), ms(l[len(l)/2]), ms(l[len(l)*95/100]), ms(l[len(l)-1]))
	}
	sorted := append([]int64(nil), perViewer...)
	sort.Slice(sorted, func(a, b int) bool { return sorted[a] < sorted[b] })
	fmt.Printf("\nstream: %d cell events delivered, per viewer min %d, median %d, max %d\n", events.Load(), sorted[0], sorted[len(sorted)/2], sorted[len(sorted)-1])
	fmt.Printf("bytes received: %.1f MB\n", float64(bytesIn.Load())/1e6)
	if len(st.errs) == 0 {
		fmt.Println("errors: none")
	}
	for e, c := range st.errs {
		fmt.Printf("error x%d: %s\n", c, e)
	}
	if len(samples) > 1 {
		var maxRSS, sumRSS int64
		for _, s := range samples {
			sumRSS += s.rssKB
			if s.rssKB > maxRSS {
				maxRSS = s.rssKB
			}
		}
		first, last := samples[0], samples[len(samples)-1]
		cpu := (last.cpu - first.cpu).Seconds() / last.at.Sub(first.at).Seconds() * 100
		fmt.Printf("\ngateway pid %d: RSS start %.1f MB, mean %.1f MB, peak %.1f MB; CPU %.1f%% of one core on average over %s\n",
			*pid, float64(first.rssKB)/1024, float64(sumRSS)/float64(len(samples))/1024, float64(maxRSS)/1024, cpu, last.at.Sub(first.at).Round(time.Second))
	}
}

func ms(d time.Duration) string { return fmt.Sprintf("%.1fms", float64(d.Microseconds())/1000) }

type sample struct {
	at    time.Time
	rssKB int64
	cpu   time.Duration
}

// sampleProcess reads RSS and cumulative CPU time from ps once a second.
func sampleProcess(ctx context.Context, pid int) []sample {
	var out []sample
	take := func() {
		b, err := exec.Command("ps", "-o", "rss=,time=", "-p", strconv.Itoa(pid)).Output()
		if err != nil {
			fmt.Fprintln(os.Stderr, "ps:", err)
			return
		}
		f := strings.Fields(string(b))
		if len(f) != 2 {
			return
		}
		rss, _ := strconv.ParseInt(f[0], 10, 64)
		out = append(out, sample{at: time.Now(), rssKB: rss, cpu: parseCPU(f[1])})
	}
	take()
	t := time.NewTicker(time.Second)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			take()
			return out
		case <-t.C:
			take()
		}
	}
}

// parseCPU reads ps TIME: [[dd-]hh:]mm:ss[.ss].
func parseCPU(s string) time.Duration {
	if i := strings.Index(s, "-"); i >= 0 {
		days, _ := strconv.Atoi(s[:i])
		return time.Duration(days)*24*time.Hour + parseCPU(s[i+1:])
	}
	parts := strings.Split(s, ":")
	var total float64
	for _, p := range parts {
		v, _ := strconv.ParseFloat(p, 64)
		total = total*60 + v
	}
	return time.Duration(total * float64(time.Second))
}
