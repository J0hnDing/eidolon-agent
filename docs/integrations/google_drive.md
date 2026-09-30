# Google Drive integration

Google Drive exposes exactly four trusted integration functions: `google_drive.search`, `google_drive.download`, `google_drive.upload`, and `google_drive.read`. Calls use the existing integration catalog, manifest declaration, persistent `integration_access` authorization, and per-operation risk policy. Callers cannot provide Drive URLs, API methods, headers, or credentials.

## Setup

Enable the Google Drive API in the same Google Cloud project as the existing shared Web application OAuth client. Add this authorized redirect URI:

```text
http://localhost:8000/settings/integrations/google-drive/oauth/callback
```

In Eidolon Settings, configure the shared Google OAuth client and connect Drive separately from Calendar and Gmail. Drive requests `openid`, `email`, and `https://www.googleapis.com/auth/drive`. The full Drive scope supports searching, reading, and uploading across the user's files and is a restricted Google scope; external use may require Google's verification and security requirements. Drive has its own single-use OAuth state, refresh token, account identity, and connection record. Refresh tokens stay in the OS secret store under `google_drive`; access tokens are never persisted. Replacing the Drive account invalidates prior Drive integration authorizations. Removing the shared OAuth client requires all three Google services to be disconnected.

## Functions

| Function | Input | Result |
| --- | --- | --- |
| `google_drive.search` | Optional `query`, `parent_id`, `mime_type`, `limit` (1–100). `parent_id` alone lists a folder. | Up to `limit` file records containing `id`, `name`, `mime_type`, `parents`, `modified_time`, `web_url`. Search matches file names and excludes trashed files. |
| `google_drive.download` | `file_id`, optional `export_format`. | A local Eidolon file reference (`path`, `filename`, `bytes`, `media_type`) and Drive metadata. Saves into `runtime/act/knowledge/google drive download` under a unique sanitized filename. |
| `google_drive.upload` | Local Eidolon `file` reference, optional `name`, `parent_id`. | Drive file metadata. The trusted backend reads a file from `workspace/downloads` or `knowledge/google drive download`, checks its reference and 25 MiB cap, and sends it through Drive's authenticated multipart upload endpoint. |
| `google_drive.read` | `file_id`. | Drive metadata and up to 200,000 characters of extracted text. Retrieves and saves the file first, then invokes the shared local document reader. |

Normal files download through authenticated Drive `files.get?alt=media`. Google Docs export to DOCX by default, Sheets to XLSX, and Slides to PPTX; `export_format` may request another supported format (`pdf`, plus `txt` for Docs or `csv` for Sheets). Downloads support Eidolon's existing PDF, Office, UTF-8 text, and common image types. `read` accepts readable text/PDF/Office files and reports unsupported or textless files as such. Binary downloads never pass through the generic URL downloader.

The client bounds provider responses, rejects redirects, validates downloaded file content and names, and keeps provider details and OAuth material out of normalized results and audit entries. There is no sharing, deletion, trash, move, rename, revisions, comments, or native editor mutation.
