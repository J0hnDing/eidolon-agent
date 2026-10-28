package main

import (
	"context"
	"database/sql"
	"encoding/hex"
	"net/url"
	"path/filepath"

	_ "github.com/ncruces/go-sqlite3/driver"
	_ "github.com/ncruces/go-sqlite3/vfs/adiantum"
	"go.mau.fi/whatsmeow/store/sqlstore"
	waLog "go.mau.fi/whatsmeow/util/log"
)

// The VFS encrypts the database, journals and temporary files. The key arrives
// over the parent's private pipe; neither the DSN nor SQL errors are logged.
func openAuth(ctx context.Context, directory string, key []byte) (*sqlstore.Container, error) {
	if len(key) != 32 {
		return nil, fail("invalid_input")
	}
	path := filepath.ToSlash(filepath.Join(directory, "whatsmeow-auth.sqlite"))
	// The WASM VFS expects file:C:/... on Windows, not file:///C:/....
	u := url.URL{Path: path}
	q := url.Values{"vfs": {"adiantum"}, "hexkey": {hex.EncodeToString(key)}, "_pragma": {
		"foreign_keys(ON)", "busy_timeout(10000)", "synchronous(FULL)", "temp_store(MEMORY)", "secure_delete(ON)",
	}}
	db, err := sql.Open("sqlite3", "file:"+u.EscapedPath()+"?"+q.Encode())
	if err != nil {
		return nil, fail("authentication_unavailable")
	}
	db.SetMaxOpenConns(1)
	c := sqlstore.NewWithDB(db, "sqlite3", waLog.Noop)
	if err = c.Upgrade(ctx); err != nil {
		_ = db.Close()
		return nil, fail("authentication_unavailable")
	}
	return c, nil
}
