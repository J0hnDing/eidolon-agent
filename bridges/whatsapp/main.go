// Eidolon's private WhatsApp bridge. Standard output is exclusively bounded JSON.
package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/signal"
	"regexp"
	"sync"
	"syscall"

	"eidolon/whatsapp/internal/cache"
)

const maxFrame = 4 * 1024 * 1024

type bridgeError struct{ Code string }

func (e *bridgeError) Error() string { return "WhatsApp operation could not be completed" }
func fail(code string) error         { return &bridgeError{Code: code} }

var safeCode = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)

func errorCode(err error) string {
	var e *bridgeError
	if errors.As(err, &e) && safeCode.MatchString(e.Code) {
		return e.Code
	}
	var ce *cache.Error
	if errors.As(err, &ce) && safeCode.MatchString(ce.Code) {
		return ce.Code
	}
	return "internal_failure"
}

type frameWriter struct {
	mu  sync.Mutex
	out io.Writer
}

func (w *frameWriter) write(value any) error {
	data, err := json.Marshal(value)
	if err != nil || len(data)+1 > maxFrame {
		return fail("response_too_large")
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	_, err = w.out.Write(append(data, '\n'))
	return err
}

type request struct {
	ID     json.RawMessage `json:"id"`
	Method string          `json:"method"`
	Input  map[string]any  `json:"input"`
}

func parseRequest(line []byte) (*request, error) {
	var r request
	if json.Unmarshal(line, &r) != nil || r.Input == nil || len(r.ID) == 0 || len(r.ID) > 130 || r.Method == "" {
		return nil, fail("invalid_request")
	}
	var id any
	if json.Unmarshal(r.ID, &id) != nil {
		return nil, fail("invalid_request")
	}
	switch id.(type) {
	case string, float64:
	default:
		return nil, fail("invalid_request")
	}
	return &r, nil
}

func serve(in io.Reader, out io.Writer) {
	w := &frameWriter{out: out}
	b := newBridge(func(data map[string]any) { _ = w.write(map[string]any{"event": "status", "data": data}) })
	defer b.close()
	rd := bufio.NewReaderSize(in, maxFrame)
	for {
		line, readErr := rd.ReadSlice('\n')
		// An incomplete or oversized frame must never execute a partial send.
		if readErr != nil || len(line) > maxFrame {
			return
		}
		r, err := parseRequest(line)
		if err != nil {
			if w.write(map[string]any{"id": nil, "error": map[string]any{"code": "invalid_request"}}) != nil {
				return
			}
			continue
		}
		result, err := b.request(r.Method, r.Input)
		response := map[string]any{"id": r.ID}
		if err != nil {
			response["error"] = map[string]any{"code": errorCode(err)}
		} else {
			response["result"] = result
		}
		if err = w.write(response); err != nil {
			return
		}
	}
}

func main() {
	// Abrupt panics must never serialize a provider payload or secret stack data.
	defer func() {
		if recover() != nil {
			os.Exit(1)
		}
	}()
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	go func() { <-signals; _ = os.Stdin.Close() }()
	serve(os.Stdin, os.Stdout)
}
