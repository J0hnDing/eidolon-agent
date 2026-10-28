# WhatsApp bridge

One Go/Whatsmeow child is owned by Eidolon's backend. It communicates through
bounded private NDJSON over stdin/stdout and opens no HTTP listener.

Build once with `./build.ps1`; Go 1.27.3 is required. Dependencies are pinned in
`go.mod` and `go.sum`. Run `go test ./...` and `go vet ./...` before rebuilding.
The ignored executable is `runtime/bin/eidolon-whatsapp-bridge.exe` on Windows.
The backend never builds or installs dependencies at startup.

`auth.go` opens an encrypted SQLite VFS for the provider's credentials, Signal
keys and WASA state. The random key is supplied only through private stdin.
`internal/cache` keeps normalized available history for 30 days in the existing
cache schema. `rich_text.go` projects known text primitives without retaining
the original rich JSON, media or recipient metadata.

Setup requests are private transport methods; they are not integration functions.
Public function contracts and all approval checks remain owned by Python.
`execute` sends use account identity and a durable dispatch identity. Interrupted
or uncertain sends are never automatically repeated.

Pairing, migration, retention and live-validation limitations are documented in
[the integration guide](../../docs/integrations/whatsapp.md). Whatsmeow is an
MPL-2.0 dependency; its source is unmodified.
