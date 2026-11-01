# Configure sync sources

Sync sources pull documents from external services (Google Drive, S3/MinIO, a
public website, a Git repository, a SharePoint site, a OneDrive or a folder on an
SFTP server) into knowledge collections on their own. Each source stores a
connector type, a target collection, connector-specific settings, a sync mode, an
optional schedule, and the id of the [vault secret](../secrets.md) that
authenticates it - a website needs none.

When a sync runs, the connector lists remote files, downloads them to a
temporary directory, and feeds them through the standard ingestion pipeline
(parse, chunk, embed, store). When the listing is complete, documents the source
brought in earlier and no longer lists are removed. A `SyncLog` entry records the
outcome of every sync operation.

### Architecture at a glance

| Component | Location | Role |
|-----------|----------|------|
| `BaseSyncConnector` | `app/services/rag/connectors/__init__.py` | Abstract base for all connectors |
| `RemoteFile` | `app/services/rag/connectors/__init__.py` | Pydantic model describing a remote file |
| `CONNECTOR_REGISTRY` | `app/services/rag/connectors/__init__.py` | Maps connector type strings to classes |
| `SyncSource` (DB model) | `app/db/models/sync_source.py` | Persists source configurations |
| `SyncLog` (DB model) | `app/db/models/sync_log.py` | Tracks individual sync operations |
| `SyncSourceService` | `app/services/sync_source.py` | Business logic for CRUD + trigger |
| RAG CLI commands | `app/commands/rag.py` | CLI interface for managing sources |
| RAG API routes | `app/api/routes/v1/rag.py` | REST API for managing sources |

## Quick start -- CLI

### List available connector types

```bash
# Lists the configured sources and the connector type of each
uv run agenticos cmd rag-sources
```

To see which connector types this deployment has (`gdrive`, `s3`, `web`, `git`,
`sharepoint`, `sftp`), use `GET /api/v1/rag/sync/connectors`, as in [Example:
discover available connectors](#example-discover-available-connectors).

### Add a Google Drive source -- sync every 2 hours

```bash
uv run agenticos cmd rag-source-add \
  --name "Legal docs" \
  --type gdrive \
  --org 0c8f2b1e-... \
  --collection legal \
  --config '{"folder_id": "1abc123def", "include_subfolders": true}' \
  --sync-mode new_only \
  --schedule 120
```

### Add an S3 source -- manual sync only

```bash
uv run agenticos cmd rag-source-add \
  --name "Marketing" \
  --type s3 \
  --org 0c8f2b1e-... \
  --collection marketing \
  --config '{"bucket": "my-docs", "prefix": "marketing/"}' \
  --sync-mode full \
  --schedule 0
```

### Add a Git source -- a repository's docs, nightly

```bash
uv run agenticos cmd rag-source-add \
  --name "Handbook" \
  --type git \
  --org 0c8f2b1e-... \
  --collection handbook \
  --config '{"repository_url": "https://github.com/acme/handbook.git", "branch": "main", "path_prefix": "docs"}' \
  --sync-mode new_only \
  --schedule 1440
```

Then choose its access token as the source's credential in the UI, or send
`secret_id` with a `PATCH` — see [Git repository setup](#git-repository-setup).

### Add a SharePoint source -- one library folder, every 6 hours

```bash
uv run agenticos cmd rag-source-add \
  --name "HR policies" \
  --type sharepoint \
  --org 0c8f2b1e-... \
  --collection hr \
  --secret-id <vault-secret-id> \
  --config '{"site_url": "https://contoso.sharepoint.com/sites/HR", "folder_path": "Policies"}' \
  --sync-mode new_only \
  --schedule 360
```

`--secret-id` is the Microsoft Entra app in the organization's Vault — see
[SharePoint and OneDrive setup](#sharepoint-and-onedrive-setup).

### Add an SFTP source -- one folder, nightly

```bash
uv run agenticos cmd rag-source-add \
  --name "Partner reports" \
  --type sftp \
  --org 0c8f2b1e-... \
  --collection partner-reports \
  --secret-id <vault-secret-id> \
  --config '{"host": "sftp.example.com", "root_path": "/outgoing", "host_key": "ssh-ed25519 AAAAC3Nz..."}' \
  --sync-mode new_only \
  --schedule 1440
```

`--secret-id` is the SSH login in the organization's Vault, and `host_key` is the
server's public host key — see [SFTP setup](#sftp-setup).

### Trigger sync manually

```bash
# Sync a single source by ID
uv run agenticos cmd rag-source-sync <source-id>

# Sync all active sources
uv run agenticos cmd rag-source-sync --all
```

### Remove a source

```bash
uv run agenticos cmd rag-source-remove <source-id>
```

The `<source-id>` is a UUID printed when you create the source and shown
in the `rag-sources` listing.

## Quick start -- UI

1. Navigate to **Knowledge Base** and open the **Sync** tab.
2. Click **"+ Add Source"**.
3. Select a connector type (Google Drive, S3, Website, Git
   repository, SharePoint & OneDrive, SFTP server). The form fields are
   generated from the JSON Schema of the connector's `CONFIG_MODEL`. A website
   has no credential step.
4. Fill in the connector-specific config fields (e.g. folder ID, bucket
   name).
5. Choose a target collection, sync mode, and schedule interval.
6. Click **"Create Source"**.
7. Use the **"Sync Now"** button to trigger an immediate sync, or wait
   for the schedule to fire automatically.

To change a source later, click the pencil on its row. The same wizard opens
with the source's values filled in. The connector cannot be changed, and
**"Save Changes"** sends only the fields you changed. The source stays filed
under the knowledge base it is on.

The UI calls the same REST API documented below, so anything you can do
in the UI you can also do with `curl` or any HTTP client.

## Sync modes

| Mode | Behavior |
|------|----------|
| `full` | Re-sync everything. All files are (re-)ingested, existing documents replaced. It ignores revisions and hashes, so run it when a connector's revision can miss a change. |
| `new_only` | Add new files + update changed files. Uses the connector's revision, then the SHA-256 hash, to detect changes — unchanged files are skipped. |
| `update_only` | Only update files already in the collection. New files are skipped. Uses the connector's revision, then the SHA-256 hash, to skip unchanged files. |

!!! tip "`new_only` for most workflows"

    It adds new files and updates modified ones while skipping unchanged files,
    which is the fastest incremental sync. `update_only` refreshes existing
    documents without adding new ones; `full` is a clean re-import every time.

### What a sync removes

In every mode, a sync removes the documents its source brought in earlier and no
longer lists: a page taken off the site, a file deleted from the Drive folder, an
object removed from the bucket. The sync log counts them under `removed`.

It removes nothing unless the listing was **complete**. A crawl that stopped at its
page limit, or could not read one of the pages, has not seen what it does not list.
That run keeps every document and says so in the sync log's message. The next sync
with a complete listing removes what is gone. A document that could not be removed
counts as a failed file, and the next sync tries again.

One sync of a source runs at a time. A sync started while another sync of the same
source is still running does not start, and its log says so.

Only the source's own documents are removed. An upload, or a document another
source brought into the same collection, is never touched. When two sources on
one collection list the same document, it stays until both stop listing it. A
document ingested before its source recorded this (September 2026) is kept until
a sync of the source lists it again.

### What a second sync does

A sync after the first one does as little as the source lets it:

- **An unchanged file costs at most a download, never an embedding.** Its SHA-256
  matches the stored document's, so it is counted as `skipped` and never parsed or
  embedded again. A connector that reports a revision for each file saves the
  download too. S3 reports each object's `ETag`, so `new_only` and `update_only`
  skip an object with an unchanged `ETag` before the download. An SFTP server
  reports each file's size and modification time, so the same modes skip a file
  whose size and time are unchanged. That revision misses an edit that keeps both.
  `full` downloads every file, and reads such an edit.
- **An unchanged source costs one request.** A connector that can say what its
  whole content is at — a Git branch's head commit, a SharePoint library's change
  feed — records that after every run that finished with nothing failed. The next `new_only` or `update_only` run that
  finds the same value, under the same configuration, stops before it lists
  anything: its log shows no files processed, and it removes nothing. Changing the
  configuration, the collection or the mode makes the next run read everything
  again, and `full` never stops early.
- **A file a stopped sync left half-done is put right.** A worker that stopped
  after storing a file's vectors, and before recording them, leaves them untracked.
  The next sync of that source deletes what nothing tracks and ingests the file
  again, and it does not stop early while such a file is waiting.

A run with a failed file records no state, so the next run reads the source in
full and retries it.

## Schedule

The `schedule_minutes` field controls how often the source syncs
automatically:

| Value | Meaning |
|-------|---------|
| `0` (or `null`) | Manual only -- trigger via CLI or UI |
| `30` | Every 30 minutes |
| `120` | Every 2 hours |
| `1440` | Once per day |

!!! warning "A schedule needs the worker"

    The `dispatch-due-syncs` schedule wakes every 60 seconds and queues a sync
    for each source that has come due, so `schedule_minutes` does nothing without
    the `worker` container `make dev` starts. Without it, a manual trigger (CLI,
    API or the UI) queues its sync too, and the sync waits for a worker as well.
    A source never has two syncs running at once.

## Google Drive setup

### 1. Create a service account

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (or select an existing one).
3. Enable the **Google Drive API**.
4. Go to **IAM & Admin > Service Accounts** and create a new service
   account.
5. Create a JSON key for the service account and download it.

### 2. Share your Drive folder

1. Open Google Drive and navigate to the folder you want to sync.
2. Click **Share** and add the service account email address (it looks
   like `name@project.iam.gserviceaccount.com`).
3. Grant at least **Viewer** access.

### 3. Give the source the key

Paste the contents of the JSON key file into the source's **Service Account
JSON** field. A `gdrive` source runs on the credential its own configuration
carries and on nothing else — there is no deployment-wide fallback, because one
would let a source's `folder_id` decide what is listed under the operator's
service account.

`GOOGLE_DRIVE_CREDENTIALS_FILE` in `.env` is for the `rag-sync-gdrive` CLI
command only.

### 4. Get the folder ID

The folder ID is the last segment of the Google Drive folder URL:

```
https://drive.google.com/drive/folders/1abc123def456ghi
                                        ^^^^^^^^^^^^^^^
                                        This is the folder ID
```

### 5. Google Drive connector config fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `folder_id` | string | Yes | -- | Google Drive folder ID from the URL |
| `include_subfolders` | boolean | No | `true` | Recursively include files from subfolders |

The service account itself is **not** a config field. Add it to the Vault as a
`gcp_service_account` credential and point the source at it with `secret_id`: it is
stored once and referenced by every source that needs it, rather than pasted into
each one ([#937](https://github.com/vstorm-co/agenticos/issues/937)). Posting it
under `config` is refused.

A `folder_id` may hold only what Google issues — letters, digits, `-` and `_`.
Anything else is refused when the source is created, because the id is
interpolated into the Drive query and a single quote in it widens what the query
lists.

Google Docs, Sheets, and Slides are automatically exported to portable
formats (PDF, XLSX, PPTX) during download. A file whose Drive name contains path
separators is written as one file inside the sync directory, never at the path
its name spells.

## S3 / MinIO setup

### 1. Configure the environment

Add the following variables to your `.env`:

```bash
S3_RAG_ENDPOINT=https://s3.amazonaws.com   # or your MinIO URL, e.g. http://localhost:9000
S3_RAG_ACCESS_KEY=your-access-key
S3_RAG_SECRET_KEY=your-secret-key
S3_RAG_REGION=us-east-1                    # required for AWS, optional for MinIO
```

For MinIO, the endpoint is typically `http://minio:9000` (Docker) or
`http://localhost:9000` (local).

### 2. S3 connector config fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `bucket` | string | Yes | -- | S3 bucket name |
| `prefix` | string | No | `""` | Key prefix to limit sync scope (e.g. `documents/legal/`). Leave empty for the entire bucket. |

## Website setup

A `web` source reads a public website, usually a product's documentation site. It
needs no credential and no vault entry. Give it a start URL, and it either
follows links from that page or reads the pages a sitemap lists.

```bash
uv run agenticos cmd rag-source-add \
  --name "Product docs" \
  --type web \
  --org 0c8f2b1e-... \
  --collection product-docs \
  --config '{"root_url": "https://docs.example.com/guide/", "max_depth": 3}' \
  --sync-mode new_only \
  --schedule 1440
```

### Website connector config fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `root_url` | string | Yes | -- | The page the crawl starts from. Its host is the only host the source reads. |
| `max_depth` | integer | No | `2` | How many links away from the start URL to follow, `0` to `10`. `0` reads the start page only. |
| `path_prefix` | string | No | the start URL's folder | Only pages whose path starts with this are read. `https://docs.example.com/guide/intro` reads `/guide/` by default; set `/` for the whole host. |
| `sitemap_url` | string | No | -- | Read the pages this sitemap lists instead of following links. It must be on the start URL's host, and use `https://` when the start URL does. A sitemap index is followed to its sitemaps. |
| `max_pages` | integer | No | `500` | The crawl stops after reading this many pages, `1` to `5000`. |

### What bounds a crawl

- **One host and one path.** Links to other hosts, and to paths outside
  `path_prefix`, are not followed. A redirect that leaves them is not followed
  either. A start URL on `https://` is never left for `http://`: a link or a
  redirect to a cleartext page is not followed.
- **The deployment's network is out of reach.** Every request - robots.txt, the
  sitemap, each page and each redirect - is checked against the same SSRF policy as
  webhooks and MCP servers. It is sent to the address that passed the check. A start
  URL that resolves to a private, loopback, link-local or cloud-metadata address is
  refused when you save the source.
- **robots.txt is obeyed** for sitemaps and pages, including `Crawl-delay` up to
  ten seconds. The crawler identifies itself as `AgenticOS-Crawler`. It waits at
  least half a second between requests, and a page that says `noindex` or
  `nofollow` is honoured. A page that a sitemap still lists after it says
  `noindex`, or after it is gone, is removed from the collection.
- **Size and time.** A page larger than 5 MB is not read. The crawl stops at
  `max_pages`. A sync stops reading the site after six hours, and a sync that
  stopped removes nothing.

Each page is stored as a Markdown document holding its text and the URL it came
from, without its query string. Navigation, headers, footers and scripts are left out. A page is re-embedded
only when its text changes. A new build stamp or tracking script in the markup does
not count as a change.

### Who can read what it imports

A website source has no credential, so its reach is what the site shows to anyone
on the internet. It never gets past a login. Everything it imports is searchable by
everyone who can search the collection it feeds, as with any other source. See
[who ends up able to read what a source ingested](../file-processing.md#who-ends-up-able-to-read-what-a-source-ingested).

Only HTML pages are imported. A PDF or other file linked from a page is not
downloaded.

## Git repository setup

A `git` source reads a repository's documentation over HTTPS — GitHub, GitLab or
any other host that serves git over HTTPS. It needs the clone URL and an access
token, not either platform's API.

### 1. Issue a token for the one repository

**A token's reach is the source's reach.** Everything the source ingests becomes
searchable by whoever can read the collection, so a token that can read every
private repository its owner can is a token that can publish all of them to that
audience. See [who ends up able to read what a source
ingested](../file-processing.md#who-ends-up-able-to-read-what-a-source-ingested).

- **GitHub:** a fine-grained personal access token, *Only select repositories*,
  with the one repository, and **Contents: Read-only** as its only permission.
- **GitLab:** a project access token on the one project, role **Reporter**, scope
  **`read_repository`** only.

Give it an expiry date. When it expires, the source's next sync fails with *the
repository refused the source's token*, and the fix is a new token in the same
vault secret.

### 2. Add it to the Vault

Add the token to the Vault as a **Git access token**, with the **host** it
belongs to: `github.com`, `gitlab.com`, or your own server such as
`git.example.com:8443`. A host with non-ASCII letters is entered in its encoded
form, such as `xn--bcher-kva.example` for `bücher.example`. Then choose the token
on the source's credential step. It is sent
as an HTTP `Authorization` header, never in the URL, and never in a command line
another process can read.

**The host is the token's, not the source's.** Whoever edits a source chooses
its repository URL, and a token is sent only to the host it was added with. So
editing a source cannot aim the organization's token at another server, and no
other kind of key, such as a model provider's API key, can be chosen for a Git
source at all.

### 3. Git connector config fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `repository_url` | string | Yes | -- | The HTTPS clone URL, e.g. `https://github.com/acme/handbook.git`. No user name or token in it. |
| `branch` | string | No | `main` | The branch to read. |
| `path_prefix` | string | No | -- | A directory inside the repository, e.g. `docs`. Leave empty for the whole repository. |
| `include` | list of strings | No | `**/*.md`, `**/*.txt` | Which files to ingest, as `.gitignore`-style patterns relative to `path_prefix`. |

The default is documentation, not the whole tree: a repository's source code is
not a corpus, and ingesting it fills a knowledge base with code nobody asked to
search. Add a pattern such as `**/*.pdf` for another format the collection's
parser reads. A pattern cannot start with `!`.

Each file is a document whose address is
`git://<host>/<owner>/<repo>@<branch>/<path>`, with `:<port>` after the host when
the port is not 443. The branch is part of the address, so two sources reading two
branches of one repository into one collection keep separate documents.

### 4. What a sync transfers

The first request of every sync is `git ls-remote` for the branch — about a
kilobyte. When the head commit has not moved since the last clean run, the sync
stops there. When it has moved, the connector makes a shallow, partial, sparse
clone: one commit, and only the files the include patterns match. A monorepo's
documentation therefore costs its documentation, not its source tree.

Before the clone writes anything to the worker's disk, the connector measures
every file it would write. A file over the knowledge base's document cap
(`MAX_UPLOAD_SIZE_MB`, 50 MB by default), or more than 512 MB of files in all, is
refused, and nothing is written.

Symbolic links and submodules are not followed, and a link is not ingested as a
document.

### Network rules

The URL must be `https://`. Its host is resolved once and checked like any other
address a tenant chooses: a host that resolves to a private, loopback or
link-local address is refused when the source is saved and again when it syncs,
and git connects only to the addresses that check approved. Redirects are not
followed. A deployment behind an egress proxy (`HTTPS_PROXY`) keeps using it; the
proxy then resolves the host itself.

The worker image ships `git`. A worker built from another image needs `git`
2.37 or newer on its `PATH`.

## SharePoint and OneDrive setup

A `sharepoint` source reads one document library through Microsoft Graph: a
SharePoint site's library, or a person's OneDrive, which Microsoft 365 keeps as a
site of its own. It can read the whole library or one folder in it. It signs in
as a Microsoft Entra app registration that an administrator of your tenant creates
once.

### 1. Register an app in Microsoft Entra

In the [Microsoft Entra admin center](https://entra.microsoft.com), open
**App registrations → New registration**. Give it a name such as *AgenticOS
sync*, keep **Accounts in this organizational directory only**, and leave the
redirect URI empty. The source signs in as the app itself, not as a person.

From the app's **Overview**, note the **Application (client) ID** and the
**Directory (tenant) ID**. Under **Certificates & secrets**, add a client secret
and copy its **Value**. The value is shown once. The secret's *ID* is not what
the source needs.

### 2. Grant it one site, not the tenant

**The app's reach is the source's reach.** Everything the source ingests becomes
searchable by everyone who can read the collection it feeds. See [who ends up able
to read what a source
ingested](../file-processing.md#who-ends-up-able-to-read-what-a-source-ingested).

Under **API permissions**, add the Microsoft Graph **application** permission
**`Sites.Selected`** and grant admin consent for it. On its own, `Sites.Selected`
reads nothing. An administrator then grants the app read access to the one site
the source reads:

```http
POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions
Content-Type: application/json

{
  "roles": ["read"],
  "grantedToIdentities": [
    {"application": {"id": "<client-id>", "displayName": "AgenticOS sync"}}
  ]
}
```

That call needs `Sites.FullControl.All`, so an administrator makes it from Graph
Explorer or with PnP PowerShell's `Grant-PnPAzureADAppSitePermission`, and not
with the app itself. `GET https://graph.microsoft.com/v1.0/sites/contoso.sharepoint.com:/sites/HR?$select=id`
answers the site id. A OneDrive is granted the same way, through its site:
`contoso-my.sharepoint.com:/personal/jane_contoso_com`.

!!! danger "`Files.Read.All` or `Sites.Read.All` makes every library in the tenant readable"

    Those permissions are consented tenant-wide. A source with them still reads
    only the library it names. But whoever can edit the source can point its site
    URL at any site or OneDrive in the organization, and the next sync makes that
    library searchable by everyone who can read the collection. Nothing in this
    product can tell which permission the app was given, because a token does not
    say. Use `Sites.Selected`, and grant one app per audience.

### 3. Add it to the Vault

Add the app to the Vault as a **Microsoft Entra app**: the tenant id (or the
tenant's domain, such as `contoso.onmicrosoft.com`), the client id and the client
secret's value. Then choose it on the source's credential step. The secret is sent
only to `login.microsoftonline.com`, to sign in.

A client secret expires, after two years at most. When it has expired, the next
sync fails with *Microsoft Entra refused the app registration's credentials
(invalid_client)*. Add a new secret to the app and replace the value in the same
vault secret. Every source that uses it picks up the new value on its next sync.

### 4. SharePoint connector config fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `site_url` | string | Yes | -- | The site, e.g. `https://contoso.sharepoint.com/sites/HR`, or a OneDrive, e.g. `https://contoso-my.sharepoint.com/personal/jane_contoso_com`. Only the site: a library or page URL copied from the browser is refused. |
| `library` | string | No | the site's default library | The library's name as SharePoint shows it, e.g. `Documents`. A OneDrive has only its default library. |
| `folder_path` | string | No | -- | A folder inside the library, e.g. `Policies/HR`. Leave empty for the whole library. |
| `include_subfolders` | boolean | No | `true` | Whether to read the folders below `folder_path`. |
| `extensions` | list of strings | No | `.pdf`, `.docx`, `.md`, `.txt` | Which file types to ingest, at most 100, each at most 16 characters. Add `.pptx`, `.xlsx` or another type only if the collection's parser reads it: a file the parser cannot read counts as a failed file on every sync. |

Each file is a document whose address is `sharepoint://<drive-id>/<item-id>`.
The address follows the file, not its path, so a file that is renamed or moved
inside the library keeps its document. The document keeps the file name it was
ingested with until the file's content changes.

OneNote notebooks and files of other types are not ingested.

### 5. What a sync transfers

Every sync starts with Microsoft Graph's change feed for the library. When
nothing in the library has changed since the last clean run, the sync stops
there: one request and no listing. The feed covers the whole library, because
Graph offers it only for a library's root. A change in another folder of the same
library therefore makes the next sync list the source's folder again.

When something has changed, the sync lists the folder and downloads every file of
the configured types. A file whose content has not changed is then skipped before
it is embedded, so only new and changed files cost an embedding. A file is
downloaded from the address Graph gives for it, without the app's token. A file
over the knowledge base's document cap (`MAX_UPLOAD_SIZE_MB`, 50 MB by default)
is not downloaded and counts as a failed file.

Graph slows down a client that reads quickly. A request that Graph throttles
(HTTP 429) or that fails with a 5xx or a network error is tried four times in all,
and the sync waits as long as Graph's `Retry-After` asks, up to a minute. A folder
that still cannot be listed is named in the sync log, and that sync removes
nothing, because the files in that folder were not seen. The files in the other
folders are still ingested.

A file deleted after the listing and before its download is removed like a file
the listing did not name.

### Network

The worker needs outbound HTTPS to `login.microsoftonline.com`,
`graph.microsoft.com` and your tenant's `*.sharepoint.com` hosts. Nothing a
source's editor types becomes an address the worker connects to: the site URL is
only a name Graph looks up. Microsoft's national clouds, such as US Government and
China, use other hosts and are not supported.

## SFTP setup

An `sftp` source reads a folder tree on an SFTP server over SSH, such as a
partner's drop folder or a file server. It logs in with an SSH login from the
Vault. It trusts only the host key in its own settings, and it reads nothing of
the worker's own SSH setup.

### 1. Get the server's host key

The host key is the only proof that the worker talks to the server the source
names. Get it from the server's administrator. Or read it from the server
yourself and compare its fingerprint with the administrator's:

```bash
ssh-keyscan -t ed25519 -p 22 sftp.example.com > host_key.txt
ssh-keygen -lf host_key.txt
```

`ssh-keyscan` reads the key over the network, so it cannot prove that the key is
the server's. `ssh-keygen -lf` prints the key's `SHA256:` fingerprint. The
administrator reads the same fingerprint on the server with
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`. If the two are not the same,
do not use the key.

`ssh-keyscan` writes the host name in front of the key. The source needs only the
key type and the key:

```bash
cut -d' ' -f2- host_key.txt
```

The connector pins a key of type `ssh-ed25519`, `ssh-ed448`,
`ecdsa-sha2-nistp256`, `ecdsa-sha2-nistp384`, `ecdsa-sha2-nistp521` or `ssh-rsa`.
For a server without an Ed25519 key, use `-t ecdsa` or `-t rsa`. The worker asks
the server only for a key of the pinned type. A server with keys of several types
answers with the key of that type.

A server with no key of the pinned type ends the first sync while the connection
opens. The sentence in the sync log depends on the server: [the connection was
lost](#sftp-the-connection-to-was-lost), [the server closed the
connection](#sftp-closed-the-connection), or [the two could not
agree](#sftp-and-this-worker-could-not-agree-on-a-host-key-or-a-cipher). For each
of the three, check the key type first.

### 2. Add an SSH login to the Vault

**The account's reach is the source's reach.** Everything the source ingests
becomes searchable by everyone who can read the collection it feeds. Ask the
administrator for an account that can read only the folder the source reads. See
[who ends up able to read what a source
ingested](../file-processing.md#who-ends-up-able-to-read-what-a-source-ingested).

Also ask the administrator to lock the account in a folder that holds only what
may be synced: `ChrootDirectory` on OpenSSH, `DefaultRoot` on ProFTPD. This is
necessary on every server, because a link put in the folder can make the sync read
another file that the account can read. A chroot that also holds other files still
exposes them. See [what an SFTP sync transfers](#4-what-an-sftp-sync-transfers).

Prefer a key to a password, and prefer an Ed25519 key. Make a key pair for the
source:

```bash
ssh-keygen -t ed25519 -C agenticos-sync -f agenticos_sync
```

If the server does not take an Ed25519 key, make an ECDSA key instead. Apache MINA
SSHD without its EdDSA library is such a server.

```bash
ssh-keygen -t ecdsa -b 256 -C agenticos-sync -f agenticos_sync
```

The administrator adds `agenticos_sync.pub` to the account's `authorized_keys`.
The private key, `agenticos_sync`, goes into the Vault.

Add the login to the Vault for the service **SFTP server**, as an **SSH login**:

- **User name**: the account on the server.
- **Host**: where the login may be used, such as `sftp.example.com`. For a port
  other than 22, add the port: `sftp.example.com:2222`. Write an IPv6 address in
  brackets, such as `[2001:db8::7]:2222`. The Vault stores the host as a source
  spells it: in lower case, without a trailing dot, an IPv6 address in its short
  form, and a name with non-ASCII letters in its encoded form, such as
  `xn--bcher-kva.example` for `bücher.example`. A name may hold an underscore,
  such as `sftp_server`. A Git access token's host may not.
- **Private key**: the whole key file, in OpenSSH, PKCS#8 or PKCS#1 form, at most
  16,384 characters. If the key is encrypted, also give its **Key passphrase**, at
  most 1,024 characters. An RSA key may have at most
  4096 bits, and its two primes must be of the same size, as `ssh-keygen` makes
  them. A key held on a security key (`sk-`) cannot be used. An encrypted key may
  ask for at most 128 bcrypt rounds (OpenSSH), or 1,000,000 iterations and a
  64-byte derived key (PKCS#8). `ssh-keygen` uses 16 rounds.
- **Password**: only for a server that takes no key. It must be at least eight
  characters and at most 1,024. A server that takes a password only through keyboard-interactive
  login, such as PAM with `PasswordAuthentication no`, refuses it. The worker
  never answers a keyboard-interactive prompt, so use a key there.

A login holds a private key or a password, never both. The Vault opens the key
with its passphrase when you save the login, so a key that does not open is
refused at once, not at the first sync. It opens the key in a separate process
that may take at most 5 seconds. An Ed25519 key takes milliseconds. An RSA key of
4096 bits, or a key at the limits above, takes under two seconds. Then choose the
login on the source's credential step.

**The host is the login's, not the source's.** Whoever edits a source types its
host, and the connector sends the login only to the host and port it was added
for. A login added for `sftp.example.com` is used on port 22 only. So editing a
source cannot send the organization's login to another server.

!!! warning "Whoever edits the source also chooses the host key it trusts"

    An editor who can also intercept the worker's traffic to the login's host can
    pin a key of their own and receive a password. A key login does not expose the
    key: the server gets only a signature that is valid for its own session. This
    is the reason to prefer a key.

### 3. SFTP connector config fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `host` | string | Yes | -- | The server's host name or IP address, e.g. `sftp.example.com`. The host alone: no scheme, user name, port or path. |
| `port` | integer | No | `22` | The server's SSH port, `1` to `65535`. |
| `root_path` | string | Yes | -- | The absolute path of the folder to read, e.g. `/outgoing`. It has no default: on a server that does not keep the account in its home folder, `/` is the whole file system. |
| `host_key` | string | Yes | -- | The server's public host key, one line such as `ssh-ed25519 AAAA...`, from [step 1](#1-get-the-servers-host-key). A comment after the key is dropped. |
| `include_subfolders` | boolean | No | `true` | Whether to read the folders below `root_path`. |
| `extensions` | list of strings | No | `.pdf`, `.docx`, `.md`, `.txt` | Which file types to ingest, at most 100, each at most 16 characters. Add `.pptx`, `.xlsx` or another type only if the collection's parser reads it: a file the parser cannot read counts as a failed file on every sync. |

The form shows these fields as **Host**, **Port**, **Folder**, **Host key**,
**Include subfolders** and **File types**. The connector writes each folder in one
form: `/outgoing//2026/` and `/outgoing/./2026` both become `/outgoing/2026`. A
folder with `..` in it is refused.

Each file is a document whose address is `sftp://<user>@<host>:<port><path>`, such
as `sftp://sync@sftp.example.com:22/outgoing/2026/report.pdf`. The user name comes
from the login and is percent-encoded. The path is the file's full path on the
server. So two accounts on one server keep separate documents, and a source
changed to a narrower folder keeps the documents it still lists. A login changed
to another user name, or a source changed to another host or port, changes every
address. The next sync then reads every file again, and a complete listing removes
the documents at the old addresses. Plan a server move with that cost in mind.

### 4. What an SFTP sync transfers

SFTP has no change feed, so every sync lists the whole folder tree. The listing
gives each file's size and modification time, in whole seconds. That pair is the
file's revision, so `new_only` and `update_only` skip a file whose size and time
are unchanged, before the download.

!!! warning "An edit that keeps the size and the time is missed"

    `put -p` keeps the uploader's time, a `SETSTAT` can set the time back, and a
    rewrite of the same size can fall within one second. Each of them changes a
    file and keeps its revision, so `new_only` and `update_only` skip the file.
    Run the source in `full` mode to read every file again. OpenSSH, SFTPGo,
    ProFTPD and Apache MINA SSHD all behave this way.

A download reads the file in requests of 255 KiB, one at a time, so a 50 MB file
costs about 200 round trips. The connector compares the file's stats before and
after the read. A file that changed or was replaced during the read, or was
deleted after the listing, is counted as `skipped`. The next sync reads its new
version or removes its document.

An upload that is still in progress usually changes the file during the read, so
the sync skips the file. If no byte arrives during the read, the sync can ingest
the part that has arrived, and the next sync reads the complete file. An upload
to a temporary name, renamed when it is complete, is never read in part if the
temporary name has a type that is not in **File types**, such as `.part` or
`.filepart`.

A file over the knowledge base's document cap (`MAX_UPLOAD_SIZE_MB`, 50 MB by
default) is not downloaded. It counts as a failed file on every sync, so it stays
visible. The connector also counts the bytes during the read, so a server that
sends more than its stats claimed cannot pass the cap.

The listing leaves out these entries:

- **Links and special files.** The listing never lists a symbolic link,
  whatever it points at, so the sync walks no loop. A FIFO, a socket or a device
  is not listed either. A link can still replace a listed file, or a folder,
  after the listing: see the warning below.
- **Names that cannot be an address.** A name that is empty, holds `/` or a NUL
  byte, is longer than 255 bytes or is not UTF-8, is left out. So is a name whose
  path is longer than 4,096 bytes. The sync log names the folder, and the listing
  stays complete.
- **Entries of unknown type.** An entry whose type the server does not give is
  left out. The listing is then incomplete, so that run removes nothing.
- **Folders the server does not list.** A folder below `root_path` that the
  server refuses to list, usually for lack of permission, or answers in a way the
  worker cannot decode, is named in the sync log and in the worker log. That run
  removes nothing.

!!! warning "A link put in the folder can make the sync read another file"

    SFTP version 3 cannot open a path without following a link, and no server
    tested offers a way around it. A person who can write in the folder can
    replace a listed file with a link after the listing. The download then reads
    the link's target, if the account can read it, and the sync ingests it as the
    listed file.

    This race exists on every server. Where the server's stat reports a link as
    a link, as on OpenSSH, SFTPGo and Apache MINA SSHD, the link must be put in
    during a few requests before the download, and removed again before the
    download ends. The stored revision is then the listed file's, so `new_only`
    and `update_only` keep the wrong content until the listed file changes. On
    ProFTPD, whose stat follows links, the window runs from the listing to the
    download, and the link can stay.

    A folder can be replaced the same way, on every server, and that link can
    stay. A stat of a path follows a link in each folder of the path, and checks
    only the last name. If a person replaces a folder with a link after the sync
    listed the folder above it, the sync lists the link's target in its place,
    and ingests those files under the folder's path. No check of the connector
    sees it.

    The fix is on the server:

    - The account can read only what may be synced: a chroot whose content is
      the synced tree (`ChrootDirectory` with `ForceCommand internal-sftp` on
      OpenSSH, `DefaultRoot` on ProFTPD), or an account whose permissions reach
      only that tree. A chroot that also holds other files still exposes them
      through a link. On Apache MINA SSHD, a virtual root does not stop a link,
      so confine the account in the operating system. SFTPGo refuses a link that
      points outside the user's root.
    - The accounts that upload cannot make links or hard links:
      `internal-sftp -P symlink,hardlink` on OpenSSH, `<Limit SYMLINK> DenyAll`
      and `SFTPExtensions -hardlink` on ProFTPD, and no `create_symlinks`
      permission on SFTPGo. On ProFTPD, `<Limit SYMLINK>` alone does not stop a
      hard link.
    - A hard link gets past every check and a chroot. The Linux setting
      `fs.protected_hardlinks=1`, the default on common distributions, stops a
      user from linking a file that they do not own and cannot read and write.

    The connector also checks that the file it opened is the file it listed. The
    owner and the group must be those of the listing. The permissions must be
    those of the stat just before the open, and also those of the listing when
    that stat gives another size or modification time than the listing. A
    listing's permissions can be faked, as ProFTPD's `DirFakeMode` does, so an
    unchanged file there still reads. A file that really changed on such a
    server fails once, and the next sync reads it.

    This is defence in depth, not a fix, and it covers a replaced file only. On
    OpenSSH and ProFTPD it catches a swap to a file with another owner, group or
    permissions, and the file fails with "The server opened a file for … that is
    not the file it listed. …".

    The check does nothing on Apache MINA SSHD and SFTPGo, which answer it from
    the path. It also misses a hard link, a target with the same owner, group
    and permissions, and a folder replaced with a link before the sync listed it.
    On ProFTPD it misses a link put in before the stat to a file with the listed
    owner, group, size and modification time.

The worker log is the output of the worker process. In a compose deployment, read
it with `docker compose logs worker`. It holds the server's own answer, which the
sync log never shows, and the reason a file failed.

Each sentence that the listing adds to the sync log counts as a failed file. So
the run has the status `error` until the cause is removed on the server, or until
a narrower folder leaves it out. The sync log shows at most three notes, and
counts the others as "And … more.". Correct the causes it shows and sync again to
see the next ones.

A control character in a file or folder name, such as a line break or a Unicode
line or paragraph separator (U+2028, U+2029), shows as `?` in the sync log and in
the connector's own sentences in the worker log. The file is still synced under
its own name. The worker's `Failed to sync …` line shows the name as the log
filter escapes it, such as `\n` for a line break.

A sync uses one connection for the listing and every download. The worker sends a
keepalive every 30 seconds, and a server that misses three is taken as gone. A
connection or an SFTP session that the server closed between two files is opened
again for the next file.

The listing reads up to 4 folders at once on that connection, at about 4 round
trips a folder. A small file then costs about 7 round trips: two stats of the
path, the open, two stats of the open file, one read and the close. At 80 ms a
round trip, that is about half a second a file, before the parse and the
embedding.

A sync may run for 4 hours, so a first import of a large tree over a
slow link can take more than one run. Each run keeps what the runs before it
ingested, and `new_only` and `update_only` skip those files before the download,
so the next run goes on where the last one stopped.

A connect that fails is not tried again in the same sync. This includes a refused
login, a timeout and a connection lost before the SFTP session started. Each file
after it fails with the same sentence, and no second login is sent. So a changed
password costs one refused login for each sync, not one for each file. Each sync
still sends one, so a short schedule or repeated manual syncs can reach the
server's lockout limit. When the login is refused, correct it or pause the source.

!!! warning "One wrong login can stop the other sources on an OpenSSH server"

    OpenSSH 9.8 and later refuses connections from an address for a time after
    failed logins from it (`PerSourcePenalties`, on by default). All sources of
    a deployment usually connect from one address, the workers'. So one source
    with a wrong password can make the syncs of other sources on the same server
    fail with [the connection was lost](#sftp-the-connection-to-was-lost),
    although their own logins are correct.

### What bounds an SFTP sync

Fixed bounds protect the worker from a broken or hostile server. They are not
settings. A listing that reaches one of the first six stops short, names the bound
in the sync log, and removes nothing. Choose a narrower folder until a sync
finishes without the message.

| Bound | Value | When it is reached |
|---|---|---|
| Folder levels below `root_path` | 32 | The listing stops |
| Folders in the listing | 10,000 | The folders past it are not read |
| Names in the listing, `.` and `..` included | 1,000,000 | The listing stops |
| Files of the configured types in the listing | 100,000 | The listing stops |
| The memory the listed files' paths take | 64 MB, about one byte a character for an ASCII path and up to four for other characters | The listing stops |
| The whole listing | 30 minutes | The listing stops |
| One name, and one path | 255 bytes, and 4,096 bytes | The name is left out |
| The connect and the login | 30 seconds | The connect fails |
| One SFTP request | 30 seconds | The worker drops the connection. A request of the listing fails the sync, and a request of a download fails that file |
| One file's download | 10 minutes | The file fails |
| One file's size | `MAX_UPLOAD_SIZE_MB`, 50 MB by default | The file fails |

### SFTP network rules

The source's host is resolved and checked like any other address a tenant chooses.
A host that resolves to a private, loopback, link-local, CGNAT or reserved address
is refused when the source is saved and again when it syncs. The worker then
connects only to the addresses that passed the check, and never resolves the name a
second time.

A file server on the company network has a private address. An operator can list
it in `SYNC_ALLOWED_HOSTS` (see [project settings](../configuration.md#project-settings)).
Set it for the API and for the worker. The API resolves and checks the host when
a source is saved, and the worker does it again when the source syncs, so both
must also be able to resolve the name.

A listed host may resolve to a private or CGNAT address. It may never resolve to a loopback, link-local, multicast or cloud
metadata address. The list is for the whole deployment: every organization's
sources can reach every port of a listed host. Only SFTP sources read it. Git and
website sources keep the plain address check.

Write each entry as a source's **Host**: a name or an IP address alone. The list
matches the host that the source types, not the address it resolves to. For a
source whose **Host** is `files.corp.example`, list `files.corp.example`: its
address `10.0.0.5` alone does not allow it. An entry with a port, a scheme, a path, a range, a wildcard or a trailing dot stops the
settings from loading, and the startup error names the entry. So does an entry
that reads as a loopback, link-local or metadata host once it is folded as a
source's host is, such as `localhost` in full-width letters.

Every organization can add a login of its own for a listed host and try it there.
An organization without a valid login still learns whether the host answers SSH
and whether its host key matches. So list only a server whose accounts are the
access control.

The worker connects to the server's SSH port directly. An HTTP proxy such as
`HTTPS_PROXY` does not apply to it.

The worker reads none of its own SSH setup:

- `~/.ssh/config`, `~/.ssh/known_hosts` and the keys in `~/.ssh`
- an SSH agent at `SSH_AUTH_SOCK`
- a Kerberos ticket, whose key exchange skips the host key check

The pinned key is the only host key the worker trusts. The worker tries only the
one login method the Vault login holds, and never keyboard-interactive. A server
that lets the worker in without a login is refused, because that server checked
nothing.

Compression is off. asyncssh puts no bound on what a compressed packet inflates
to, so a server could make the worker use hundreds of megabytes for each packet.
A server that offers only compressed connections is refused with [the key
exchange sentence](#sftp-and-this-worker-could-not-agree-on-a-host-key-or-a-cipher).

!!! note "A worker needs a local user name"

    asyncssh calls `getpass.getuser()` on every connect, and nothing turns that
    off. The name is not sent. The shipped image has a passwd entry for its user.
    A worker that runs as a user with no passwd entry needs the `LOGNAME`
    variable. This applies to an image of your own, and to the shipped image
    started with another user id, for example with `docker run --user` or a
    Kubernetes `runAsUser`. Without it, every SFTP sync fails, and the sync log
    shows "The sync did not finish (OSError) - check the source's credential,
    then run it again. …". The credential is not the cause.

## API reference

All sync source endpoints live under `/api/v1/rag/sync/`. Listing takes
`collections:view` and everything that changes a source takes `collections:edit`,
in both cases reaching the collection the source belongs to — there is no admin
role in it. See
[who may reach a collection](../file-processing.md#who-may-reach-a-collection).

### Sync sources CRUD

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/rag/sync/sources` | List all configured sync sources |
| `POST` | `/api/v1/rag/sync/sources` | Create a new sync source |
| `PATCH` | `/api/v1/rag/sync/sources/{id}` | Update an existing sync source |
| `DELETE` | `/api/v1/rag/sync/sources/{id}` | Delete a sync source |
| `POST` | `/api/v1/rag/sync/sources/{id}/trigger` | Manually trigger a sync |

### Connectors & logs

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/rag/sync/connectors` | List available connector types with config schemas |
| `GET` | `/api/v1/rag/sync/logs` | List sync history (filterable by `collection_name`) |

### Example: create a source via API

```bash
curl -X POST http://localhost:8000/api/v1/rag/sync/sources \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Legal Drive",
    "connector_type": "gdrive",
    "collection_name": "legal",
    "config": {
      "folder_id": "1abc123def",
      "include_subfolders": true
    },
    "sync_mode": "new_only",
    "schedule_minutes": 120
  }'
```

### Example: trigger a sync via API

```bash
curl -X POST http://localhost:8000/api/v1/rag/sync/sources/{source_id}/trigger \
  -H "Authorization: Bearer $TOKEN"
```

### Example: check sync history

```bash
curl http://localhost:8000/api/v1/rag/sync/logs?limit=10 \
  -H "Authorization: Bearer $TOKEN"
```

### Example: discover available connectors

```bash
curl http://localhost:8000/api/v1/rag/sync/connectors \
  -H "Authorization: Bearer $TOKEN"
```

The response includes each connector's `config_schema`, which the
frontend uses to render dynamic forms. It is also useful for building
integrations programmatically.

## Updating a source

You can update any subset of fields on an existing source with `PATCH`:

```bash
curl -X PATCH http://localhost:8000/api/v1/rag/sync/sources/{source_id} \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "sync_mode": "full",
    "schedule_minutes": 60,
    "is_active": false
  }'
```

Updatable fields: `name`, `config`, `sync_mode`, `schedule_minutes`,
`is_active`, `collection_name`.

Set `is_active` to `false` to pause a source without deleting it.

## Monitoring sync operations

Every sync creates a `SyncLog` entry with the following fields:

| Field | Description |
|-------|-------------|
| `source` | Connector type or `"local"` for CLI ingestion |
| `collection_name` | Target collection |
| `status` | `running`, `done`, or `error` |
| `mode` | `full`, `new_only`, or `update_only` |
| `total_files` | Number of files discovered |
| `ingested` | Successfully ingested (new) |
| `updated` | Successfully re-ingested (replaced) |
| `skipped` | Skipped: already present or unchanged, new in `update_only`, or changed or deleted during the run and left for the next run |
| `failed` | Failed to ingest, including pages or files the listing could not read, and documents that could not be removed |
| `removed` | Removed because the source no longer lists them (see [what a sync removes](#what-a-sync-removes)) |
| `error_message` | What went wrong, or why nothing was removed. A run can be `done` and still have a message, for example when a crawl stopped at its page limit, or when files changed or were deleted during the run and were left for the next run. Read `status` to know if the run failed |
| `started_at` | When the sync started |
| `completed_at` | When the sync finished |

View logs via CLI output or the API:

```bash
curl http://localhost:8000/api/v1/rag/sync/logs?collection_name=legal&limit=5 \
  -H "Authorization: Bearer $TOKEN"
```

## Adding custom connectors

To add a new connector type (e.g. Notion, Confluence, Dropbox), see
[Add a sync connector](./add-sync-connector.md).

The short version:

1. Create a class inheriting `BaseSyncConnector` in
   `app/services/rag/connectors/`.
2. Implement `list_files()`, `_fetch()`, and optionally `validate_config()`.
3. Declare `SECRET_KIND` — what kind of vault secret authenticates it — and a
   `CONFIG_MODEL`, a Pydantic model saying how to find the documents. The
   credential is never one of its fields.
4. Register it in `CONNECTOR_REGISTRY` in
   `app/services/rag/connectors/__init__.py`.

Once registered, the connector appears automatically in the CLI, API,
and UI.

## Troubleshooting

### "No sync sources configured"

You have not created any sources yet. Use `rag-source-add` (CLI) or
`POST /api/v1/rag/sync/sources` (API) to create one.

### "Unknown connector type"

The connector type you specified is not in `CONNECTOR_REGISTRY`. Check
available types with `rag-sources` or `GET /api/v1/rag/sync/connectors`.
Google Drive (`gdrive`) is available.
S3 (`s3`) is available.
Website (`web`) is available.
Git (`git`) is available.
SFTP (`sftp`) is available.

### Google Drive: "this source has no credential"

The source's `secret_id` is empty, or the vault secret it named has been deleted.
Add the service account JSON to the Vault and choose it on the source's credential
step — `GOOGLE_DRIVE_CREDENTIALS_FILE` does not stand in for it, and only the
`rag-sync-gdrive` CLI command reads that setting.

### "A Google Drive source needs a service account credential"

The `secret_id` names a credential of the wrong kind — an AWS key pair, say. A Drive
source takes a `gcp_service_account` and an S3 source an `aws_credentials` pair; the
wizard offers only the matching ones, so this is reachable through the API.

### Google Drive: "folder ID may contain only letters, digits, '-' and '_'"

The value is not a Drive folder id. Take it from the folder URL: it is the last
segment, and nothing else in that URL belongs in the field.

### Google Drive: "Cannot access folder"

Make sure you shared the folder with the service account email. The
service account needs at least Viewer access.

### S3: "Cannot access bucket"

Verify that `S3_RAG_ACCESS_KEY`, `S3_RAG_SECRET_KEY`, and
`S3_RAG_ENDPOINT` are set correctly in `.env`. For MinIO, ensure the
endpoint includes the port (e.g. `http://localhost:9000`).

### Website: "resolves to private/internal address"

The start URL, or the sitemap, points inside the deployment's network, or its name
resolves there. A website source reads only public addresses. To index an internal
site, publish its pages somewhere public, or upload the files directly.

### Website: "The site's robots.txt could not be read, so it was not crawled"

`/robots.txt` on the start URL's host timed out or answered with a server error
(5xx) three times in a row. The crawler does not guess what an unreachable
robots.txt would allow, so the run stops. A missing robots.txt (404) or a
forbidden one (403) means no rules, and the crawl goes ahead.

### Website: "The start URL … did not lead to an HTML page"

The start URL answered 404, redirected to another host or outside `path_prefix`,
or served something other than HTML. Open it in a browser, then use the address
it ends at as `root_url`.

### Website: "robots.txt does not allow the start URL"

The site asks crawlers to stay out of that path. Choose a start URL the site
allows, or ask the site's owner to allow `AgenticOS-Crawler`.

### Website: "… answered HTTP 403" or "… could not be reached"

The page needs a login, or the site refused the crawler. It failed after three
attempts if the answer was a timeout, 429 or 5xx. Each page like this counts as a
failed file. Nothing is removed on that run, because the pages behind it were not
seen.

### "The source could not be listed completely, so documents it may no longer hold were kept"

The listing stopped short: a crawl reached `max_pages`, some pages could not be
read, a SharePoint folder could not be listed, or an SFTP listing reached a bound
or could not read a folder or an entry. What was found was ingested, and nothing
was removed. For a website, raise `max_pages`, or narrow the crawl with
`path_prefix`, until a run finishes without this message. For SharePoint and SFTP,
see the entries for the listing below.

### Git: "The repository refused the source's token"

The token has expired, was revoked, or cannot read this repository. Issue a new
one as described in [Git repository setup](#git-repository-setup) and replace the
value of the vault secret the source uses; every source using that secret picks
it up on its next sync.

### Git: "The repository was not found, or the source's token cannot see it"

Check the clone URL first. A private repository answers *not found* rather than
*forbidden* to a token that cannot read it, so a fine-grained token issued for a
different repository reads as this.

### Git: "The repository has no branch named …"

The `branch` field names a branch the repository does not have. Its default is
`main`; an older repository's default branch may be `master`.

### Git: "… is … MB, and a synced file may be at most … MB"

A file the include patterns match is larger than the knowledge base's document
cap. Narrow `include` or `path_prefix` so that the file is left out. Nothing was
written for this sync.

### Git: "… over the … MB one sync may check out"

All the files the include patterns match are more than 512 MB together. Narrow
`include` or `path_prefix`, or split the repository into more than one source,
each with its own prefix.

### Git: "This token was added for …, and the repository is on …"

The source's repository is on a different host from the one its token was added
with. Either the URL is wrong, or the source needs a token added for that host.
Nothing was sent to the repository's host.

### Git: "A Git source needs a Git access token"

The source names a secret of another kind, such as an API key. Add the token as a
**Git access token**, with its host, and choose that one.

### "Another sync of this source is still running"

A run was triggered while another run of the same source was in progress, so it
did not start. The run in progress finishes normally; trigger again after it if
the source changed meanwhile.

### Git: "… resolves to a private address"

The repository's host resolves inside the deployment's network, so the source is
refused. A self-hosted Git server on an internal address cannot be reached by a
sync source.

### Git: "git is not installed on this worker"

The worker runs from an image without `git`. The shipped `backend/Dockerfile`
installs it; a custom image needs it added.

### Git: a sync finished with no files processed

The branch's head commit is the one the last clean run read, under the same
configuration, so there was nothing to do. Switch the source to `full` for one run
to read everything again regardless.

### SharePoint: "Microsoft Entra refused the app registration's credentials (…)"

Microsoft Entra did not sign the app in. `invalid_client` usually means the client
secret has expired or the vault holds the secret's ID instead of its value.
`unauthorized_client` or `invalid_request` usually means the tenant id or the
client id is wrong. Check the three values against the app's **Overview** and
**Certificates & secrets**, and replace them in the vault secret.

### SharePoint: "Microsoft Graph did not accept the app's token for …"

The app signed in, but its token carries no Microsoft Graph permission. Add the
**application** permission `Sites.Selected`, not a delegated one, and grant admin
consent, as described in [step 2](#2-grant-it-one-site-not-the-tenant).

### SharePoint: "Microsoft Graph denied the app access to … (accessDenied)"

The app has `Sites.Selected` but no grant on this site. Grant it `read` on the
site, as described in [step 2](#2-grant-it-one-site-not-the-tenant). If it names a
folder below the source's folder, that folder has permissions of its own that
exclude the app. The rest of the library was ingested and nothing was removed.

### SharePoint: "There is no SharePoint site at …, or the app cannot see it"

Check the site URL first: it is the site's own address, such as
`https://contoso.sharepoint.com/sites/HR`, not a library or page inside it. With
`Sites.Selected`, a site the app has no grant on can also answer this way.

### SharePoint: "The site has no document library named …"

The message lists the libraries the app can see. Copy one of those names into
`library`, or leave `library` empty for the site's default library. The default
library is named in the site's language, such as *Documents* or *Dokumente*.

### SharePoint: "The library has no folder …" or "… is a file, not a folder"

`folder_path` does not name a folder in the library. It is relative to the
library's top level, e.g. `Policies/HR`, and does not repeat the library's name.

### SharePoint: "The folder … could not be listed: …"

A folder below the source's folder could not be read. The reason follows the
colon. The other folders were ingested, and nothing was removed on that run.

### SharePoint: "Microsoft 365 stayed unavailable or kept throttling … after 4 attempts"

Graph was still throttling or failing after four tries. The next sync starts
again. If this happens on every sync, schedule the source less often, or split a
very large library into more than one source, each with its own `folder_path`.

### SharePoint: "A SharePoint source needs a Microsoft Entra app credential"

The source names a secret of another kind, such as an API key. Add the app to the
Vault as a **Microsoft Entra app** and choose that one.

### SharePoint: a sync finished with no files processed

Nothing in the library changed since the last clean run under the same
configuration, so there was nothing to do. Switch the source to `full` for one run
to read everything again regardless.

### SFTP: "Paste only the key type and the key, without the host name or the options in front of them."

**Host key** holds a line from `ssh-keyscan` or `known_hosts`, with the host name
in front of the key, or an `authorized_keys` line with options in front of it.
Remove the first field. For the file from [step 1](#1-get-the-servers-host-key),
`cut -d' ' -f2- host_key.txt` does this.

### SFTP: "Paste the server's public host key as one line, with its type first, …"

**Host key** holds something other than one public key line: a fingerprint from
`ssh-keygen -lf`, two lines, a PEM block or a damaged key. Paste the key itself,
such as `ssh-ed25519 AAAAC3Nz...`. A PEM block is refused before it is read,
because a private key pasted by mistake must not be read.

### SFTP: "The host key must be of type ssh-ed25519, ssh-ed448, …"

The key is of a type the connector does not pin, such as `ssh-dss`, a host
certificate or a security key. Get a key of one of the named types from the
server, for example with `ssh-keyscan -t ed25519`, `-t ecdsa` or `-t rsa`.

### SFTP: "The host is a host name or an IP address alone, …" or "Put the port in the Port field, …"

**Host** holds more than the host, such as `sftp://sftp.example.com/outgoing`,
`sync@sftp.example.com` or `sftp.example.com:2222`. Type only the name or the
address. Put the port in **Port** and the folder in **Folder**.

A trailing dot is refused too: remove it. A name holds only letters, digits,
hyphens, underscores and dots, such as `sftp_server` or `files-01.corp.example`.

### SFTP: a folder or a file type refused when the source is saved

- "The folder is an absolute path on the server, e.g. /outgoing." **Folder** does
  not start with `/`. Write the full path as the account sees it on the server.
- "The folder may not hold '..'. Name the folder itself." Write the folder's own
  path, without `..`.
- "The folder may not hold a line break or another control character." Remove the
  character. A paste can bring one in, also a Unicode line or paragraph separator
  (U+2028, U+2029).
- "The folder may be at most 1024 characters long." Choose a folder with a
  shorter path.
- "'…' is not a file extension, e.g. .pdf." An entry in **File types** is not a
  dot and 1 to 10 letters or digits.
- "At least one file type is needed." **File types** is empty. Add at least one
  type, such as `.pdf`.
- "At most 100 file types can be listed." **File types** has more than 100
  entries. Keep the types the collection's parser reads.
- "A file type is at most 16 characters, e.g. .pdf." An entry in **File types**
  is longer than 16 characters. Write the extension alone, such as `.pdf`.

A SharePoint source's **File types** gives the same last four sentences. The
two limits apply when a source is saved, and count the types after blanks and
repeated types are removed. A source stored with more types keeps syncing, and
is refused only when it is saved again.

### SFTP: "Blocked: '…' resolves to private/internal address '…'. …"

The host resolves inside the deployment's network. An operator can list a file
server on the company network in `SYNC_ALLOWED_HOSTS`, as described in [SFTP
network rules](#sftp-network-rules). A host that resolves to a loopback,
link-local or cloud metadata address stays refused, also when it is listed. For a
host typed as an IP address, the sentence is "Blocked: '…' resolves to a
private/internal address. SSRF protection does not allow requests to internal
networks." The cause and the fix are the same.

List the host as the source names it, not the address in the sentence. Set
`SYNC_ALLOWED_HOSTS` for the API and for the worker. A source refused when it is
saved is missing from the API's list. A source refused only when it syncs is
missing from the worker's list.

"Blocked: unable to resolve hostname …" means that the host could not be resolved
at all. Check its spelling. The API resolves the host when the source is saved,
and the worker when the source syncs, so both must be able to resolve the name.

### "A credential does not go in a source's configuration"

`config` holds a field that is a credential, such as `password`, `private_key` or
`passphrase` for an SFTP source. Add the credential to the Vault, and set the
source's `secret_id` to it.

### SFTP: "An SSH login needs a private key or a password, and not both."

The login has both a private key and a password, or neither of them. Give it one
way to log in: a private key, with its passphrase if it has one, or a password.
"A passphrase goes with a private key." means that the login has a passphrase and
a password. Remove the passphrase.

### SFTP: "The private key could not be read. …"

The Vault could not read the key when you saved the login. Paste the whole key
file, from its `-----BEGIN` line to its `-----END` line. If the key is encrypted,
give its passphrase too. "The private key could not be read with this
passphrase." means that the passphrase is wrong, or that the paste is not a key
the Vault reads.

The first `-----BEGIN … PRIVATE KEY-----` line must be one of `OPENSSH PRIVATE
KEY`, `PRIVATE KEY`, `ENCRYPTED PRIVATE KEY`, `RSA PRIVATE KEY`, `EC PRIVATE KEY`
or `DSA PRIVATE KEY`. A key in binary DER form is refused, and so is a PKCS#8 key
under a PEM `Proc-Type: 4,ENCRYPTED` header. Export such a key again in one of
these forms. For a key that `ssh-keygen` reads, `ssh-keygen -p -f <key file>`
writes it in OpenSSH form. A PuTTY key (`.ppk`) is refused too, and `ssh-keygen`
cannot read it. Export it in OpenSSH form with PuTTYgen (**Conversions** →
**Export OpenSSH key**), or with `puttygen <key>.ppk -O private-openssh -o <key
file>`.

### SFTP: "The private key is encrypted with too many key derivation rounds. …"

An encrypted key names how much work its passphrase takes to check. The Vault
refuses a key that asks for more than 128 bcrypt rounds (OpenSSH), or for more
than 1,000,000 iterations or a derived key of more than 64 bytes (PKCS#8). It
reads this from the key, before it decrypts anything. Encrypt the key again with
fewer rounds, for example with `ssh-keygen -p -a 16 -f <key file>`, and paste it
again.

### SFTP: "The private key takes too long to open. …"

The full sentence is "The private key takes too long to open. Use an Ed25519 key,
or an RSA key of at most 4096 bits.". The Vault opens each key in a separate
process, and stops it after 5 seconds. A key whose encrypted part asks for more
work than that, or a very busy API host, gives this sentence. Make an Ed25519
key, as described in [step 2](#2-add-an-ssh-login-to-the-vault), and save the
login again.

### SFTP: "The private key is an RSA key of more than 4096 bits. …"

The full sentence is "The private key is an RSA key of more than 4096 bits. Use
an Ed25519 key, or an RSA key of at most 4096 bits.". The time to check an RSA key
grows fast with its size, so the Vault refuses a key over 4096 bits without
checking it. Make an Ed25519 key, or an RSA key of 4096 bits or less, and add its
public half to the account's `authorized_keys`.

### SFTP: "The private key is an RSA key whose two primes are not of the same size. …"

The full sentence is "The private key is an RSA key whose two primes are not of
the same size. Use an Ed25519 key, or an RSA key that ssh-keygen made.". The
Vault refuses an RSA key whose two primes differ in size by more than 16 bits,
without checking it. `ssh-keygen` and OpenSSL make the two primes the same size,
and checking a key with one large prime takes a long time. Make a new key with
`ssh-keygen`, as described in [step 2](#2-add-an-ssh-login-to-the-vault), and add
its public half to the account's `authorized_keys`.

### SFTP: "The private key is held on a security key, which a sync cannot use to log in. …"

The full sentence is "The private key is held on a security key, which a sync
cannot use to log in. Use an Ed25519 key without a security key.". The key file
is an `sk-` key: a handle for a hardware key, such as one made with
`ssh-keygen -t ed25519-sk`. The worker has no hardware key, so it cannot sign
with it. Make an Ed25519 key with `ssh-keygen -t ed25519`, as described in [step
2](#2-add-an-ssh-login-to-the-vault).

### SFTP: "Other private keys are being checked now. Try again in a moment."

Two private keys are already being checked in the same API process, and the
Vault checks at most two at a time. The save is refused at once, with status 503
and the code `SERVICE_BUSY`, so that no request waits while it holds a database
connection. Save the login again after a few seconds.

### SFTP: "The private key is too long. Paste only the key file, at most 16,384 characters."

The **Private key** field holds more than 16,384 characters. A key file of an
Ed25519 key is about 400 characters, and one of a 4096-bit RSA key about 3,300.
So the paste holds more than the key, or it is not a key. Paste the private key
file alone, from its `-----BEGIN` line to its `-----END` line.

### SFTP: "The passphrase is too long: at most 1,024 characters."

The **Key passphrase** field holds more than 1,024 characters. Paste only the
passphrase that opens the key. A passphrase that is really longer cannot be
stored: encrypt the key again with a shorter one, with `ssh-keygen -p -f <key
file>`.

### SFTP: "The password is too long: at most 1,024 characters."

The **Password** field holds more than 1,024 characters. Paste only the password.
For a server whose password is longer, use a key instead, as described in [step
2](#2-add-an-ssh-login-to-the-vault).

### SFTP: "The private key could not be checked. Try again later."

The process that opens the key stopped with an error and gave no answer. This is
not a fact about the key. The API answers with status 500. Save the login again
after a short time. If it fails again, the API log has the line
`ssh_key_check_failed`. The process's exit status is in the record's `returncode`
field, which only a log handler that shows extra fields, such as Logfire, shows.
The API host must be able to start a Python process, and that process must be
able to import asyncssh.

### SFTP: "The host is a host name or an IP address with an optional port, …"

**Host** in the login is not one host with an optional port. Write a name or an
IP address, then `:` and the port if it is not 22, such as `sftp.example.com` or
`[2001:db8::7]:2222`. An IPv6 address goes in brackets. A scheme, a path, a
wildcard or a space is refused. A name holds only letters, digits, hyphens,
underscores and dots.

### SFTP: "This SFTP source has no credential. …"

The source's `secret_id` is empty, the login it named was deleted, or the worker
could not open the login. The worker checks the key's form each time it opens the
login, so a stored key that no longer passes that check gives this sentence too.
Then the worker log has `sync_source_secret_unusable`. Add an SSH login to the
Vault, as described in [step 2](#2-add-an-ssh-login-to-the-vault), or save the
key and its passphrase in the login again. Then choose it on the source's
credential step.

### SFTP: "An SFTP source needs an SSH login, and the credential it names is not one."

A source that names a credential of another kind, such as an API key, is
refused when it is saved, with "A SFTP server source needs a ssh_credentials
credential, and that one is …". This applies to the wizard, the API and
`rag-source-add`. Choose an **SSH login**. The sentence in the heading is the
connector's own check at sync time, and a source saved with an SSH login does not
give it.

### SFTP: "This SSH login was added for …, and the source names …"

The source's host or port is not the one the login was added for. Either **Host**
or **Port** is wrong, or the source needs a login added for that host. A login
added without a port is used on port 22 only. Nothing was sent to the source's
host.

### SFTP: "… did not prove that it holds the host key in this source's settings. …"

The server answered with a host key other than the one in **Host key**. The
server's key changed, for example because the server was installed again, or the
worker did not reach the real server. Compare the fingerprint with the
administrator's again, as described in [step 1](#1-get-the-servers-host-key),
before you change the key. No login was sent.

### SFTP: "… and this worker could not agree on a host key or a cipher. …"

On a first sync, check the host key type first. The worker asks the server only
for a key of the type in **Host key**, and Apache MINA SSHD answers this way when
it has no key of that type. Get the server's key of another type, as described in
[step 1](#1-get-the-servers-host-key). If the type is correct, the server accepts
none of the ciphers the worker offers. Ask the administrator which ones it allows.
A server that offers only compressed connections also gives this sentence,
because the worker never turns on compression.

### SFTP: "The connection to … was lost."

On a first sync, check the host key type first. OpenSSH and SFTPGo end the
connection this way when they have no key of the type in **Host key**.

OpenSSH 9.8 and later also refuses every connection from an address that failed
to log in a few times (`PerSourcePenalties`). All sources of a deployment usually
connect from one address, the workers'. So one source with a wrong password can
make the syncs of other sources on the same server fail with this sentence for a
time.

Look for a source on the same host whose last sync gave "The login to … did not
complete.". Correct its login, or pause it. Then sync again when the penalty has
ended: by default it grows to at most 10 minutes. The server log shows the penalty
as `srclimit_penalise`. The server's administrator can also exempt the workers'
address with `PerSourcePenaltyExemptList`. That removes the penalty for every
organization's sources on the deployment, so correct the login first.

Otherwise, the network or the server ended the connection, reset it, or ended the
SFTP session, or the server did not answer three keepalives. A listing that loses
the connection fails the whole sync, and that run removes nothing. A download
that loses it fails that file, and the next file opens a new connection.

### SFTP: "… closed the connection."

On a first sync, check the host key type first. ProFTPD ends the connection this
way when it has no key of the type in **Host key**. Otherwise, the server ended
the connection while it opened, for example because it allows no more sessions or
refuses the worker's address. The worker log has the server's own reason.

### SFTP: "The login to … as … did not complete. …"

The server refused the user name, the key or the password. Check them in the
Vault. For a key, check that the administrator added its public half to the
account's `authorized_keys`. A server that takes a password only through
keyboard-interactive login refuses a password login, so use a key there. A server
can also refuse the type of the key. Apache MINA SSHD without its EdDSA library
refuses an Ed25519 key, so try an ECDSA key there. A server that lets the worker in
without a login is refused with this sentence too.

The sync keeps this refusal, so the server gets one failed login, not one for each
file. Each later sync sends one more. On OpenSSH 9.8 and later, a few of them make
the server refuse the workers' address for a time, and the other sources on that
server then fail with [the connection was
lost](#sftp-the-connection-to-was-lost). Correct the login, or pause the source.

### SFTP: "The private key in the Vault could not be read with its passphrase."

The worker could not open the login's private key with the passphrase stored
beside it. The Vault checks the key when the login is saved, so this is rare. Save
the key and its passphrase in the login again.

### SFTP: "… has no SFTP service for …"

The server accepted the login, but it gives the account no SFTP service. The
account can be for a shell only, or the server runs no SFTP subsystem. Ask the
administrator to allow SFTP for the account.

### SFTP: "… answered the start of the SFTP session in a way this worker cannot use."

The server answered the SFTP version request in a form the worker cannot use, or
in an answer the worker cannot decode. The worker speaks SFTP version 3, as
OpenSSH, SFTPGo, ProFTPD and Apache MINA SSHD do.
The worker log has the server's own answer. Ask the administrator about the
server's SFTP settings.

### SFTP: "… did not answer within 30 seconds."

The server did not answer the connect, or one SFTP request, in 30 seconds. At the
connect, a firewall that drops the worker's packets is the usual cause. Check that
the worker can reach the server's SSH port. During the sync, the server stopped
answering. The worker drops the connection, and the listing or that file fails.
The listing reads several folders at once, so one folder that stalls fails the
whole listing with this sentence, and that run removes nothing.

### SFTP: "… could not be reached on port …"

The server refused the connection on that port, or the network has no route to it.
Check **Port**, and check that a firewall lets the worker reach the server. When
the host has more than one address, the worker tries each one before it stops.

### SFTP: "The sync did not finish (OSError) - check the source's credential, …"

On an SFTP source, the credential is usually not the cause. This sentence usually
means that the worker runs as a user with no passwd entry, so the SSH library
cannot read the worker's local user name. Set `LOGNAME` for the worker, as
described in the note "A worker needs a local user name" under [SFTP network
rules](#sftp-network-rules). The worker log has the full error.

### SFTP: "There is no folder … on …, or it is not a folder."

**Folder** names no folder that the account can see. Check the path. On a server
that keeps the account in a home folder or a chroot, the path starts from that
folder, not from the server's root. OpenSSH gives the same answer when
**Folder** names a file. Other servers give "… could not list the folder …" for a
file.

### SFTP: "The account … may not read the folder … on …"

The account has no permission to list **Folder**. Ask the administrator to give
the account read access to the folder, or choose a folder that the account can
read.

### SFTP: "… could not list the folder …"

The full sentence is "… could not list the folder …. Check that it is a folder the
account may read.". SFTPGo, ProFTPD and Apache MINA SSHD answer this way when
**Folder** names a file, so check the path first. Otherwise, the server failed
the listing for another reason, or sent an answer the worker cannot decode. The
worker log has the server's own answer.

### SFTP: "The listing of … stopped …. Choose a narrower folder."

The listing reached one of [the bounds](#what-bounds-an-sftp-sync), and the
sentence names which one: 32 folder levels, 10000 folders, 1000000 names, 100000
files, 64 MB of file paths or 1800 seconds. What the listing found was ingested,
and nothing was removed. You cannot raise a bound. Choose a narrower **Folder**, or split the tree
into more than one source, until a sync finishes without this sentence.

### SFTP: "The folder … could not be read."

The parent folder lists this folder, but the server refused to open it, usually
because the account has no permission, or sent an answer about it that the worker
cannot decode. The other folders were ingested, and
nothing was removed on that run. Give the account read access to the folder, or
move the folder out of the tree.

### SFTP: names left out of the listing

The sync log can give these sentences for a folder:

- "The server listed … names in … that are not file names, so they were left
  out." The names are empty, hold `/` or a NUL byte, or are longer than 255
  bytes, or their paths are longer than 4,096 bytes.
- "… names in … are not UTF-8, so those files were left out."
- "The server did not say what … entries in … are, so they were left out."

For the first two, rename the files on the server. The listing stays complete. For
the third, the server gave no type for some entries. The listing is then
incomplete, so that run removes nothing. Ask the administrator about the server.
The sync log shows at most three notes. Correct the folders it names and sync
again to see the next ones. A control character in a folder's name, or a Unicode
line or paragraph separator, shows as `?`.

### SFTP: a file counted as failed

The sync log counts the file under `failed`, and the other files are synced. The
worker log names the file and the reason, in a line such as `Failed to sync
report.pdf: report.pdf is 61 MB, and a synced file may be at most 50 MB.`

| Reason | Cause | What to do |
|---|---|---|
| "… is … MB, and a synced file may be at most … MB." | The file is larger than `MAX_UPLOAD_SIZE_MB`. It fails on every sync, before a byte is read | Move the file out of the folder, remove its type from **File types**, or raise `MAX_UPLOAD_SIZE_MB` |
| "The server sent more than … MB of …, and a synced file may be at most … MB." | The server sent more than the cap, although its stats gave a smaller size | Ask the administrator about the file |
| "The account … may not read …" | The file's permissions refuse the account | Give the account read access to the file |
| "The server refused to read …" | The server refused the file for another reason, or sent an answer the worker cannot decode. The worker log has its answer | Ask the administrator |
| "The server listed … as a file, and now says it is not one." | The path is now a folder, a link or a special file, or the server's answers disagree | The next listing settles it. If it fails on every sync, ask the administrator |
| "The server lists …, but answers that it does not exist." | The folder still names the file, but the server answers "no such file" for it | Ask the administrator. The server contradicts itself |
| "The server answers that … does not exist, and its folder could not be listed to confirm it." | The server answered "no such file", and then could not list the file's folder, or not within the file's 10 minutes | Check the folder's permissions. The next sync tries again |
| "The server sent … of the … bytes … holds." | The server sent fewer bytes than its stats give | The next sync tries again. If it fails again, ask the administrator |
| "The server answered a read of … with more than was asked for." | The server sent more bytes than one request asked for | Ask the administrator. The server is broken |
| "The server opened a file for … that is not the file it listed. …" | The open file has another owner or group than the listing, or other permissions than the stat before the open or, for a file whose size or time moved, the listing | See [its entry](#sftp-the-server-opened-a-file-for-that-is-not-the-file-it-listed) |
| "… did not download within 600 seconds." | The download took more than 10 minutes. A file of 50 MB needs about 85 KB/s | Check the speed of the network to the server |
| "… did not answer within 30 seconds." or "The connection to … was lost." | The server stopped answering, or the connection ended during the download | The next file opens a new connection, and the next sync reads this file |

When a new connection fails during the sync, the sync keeps that refusal. Each
file after it fails with the same sentence. See the entries for the connect
above.

### SFTP: "The server opened a file for … that is not the file it listed. …"

The full sentence is "The server opened a file for … that is not the file it
listed. Check that nobody can replace files in this folder with links.". The file
the server opened has another owner or group than the listing gave, or other
permissions than the stat just before the open. When that stat gives another size
or modification time than the listing, the permissions must also be those of the
listing. The usual cause is a link put in place of the listed file, or of a
folder above it, which can make the sync read another file.

The file is not ingested. See [the warning about
links](#4-what-an-sftp-sync-transfers), and confine the account on the server. A
file that another account uploaded again between the listing and the download
gives this sentence once, and the next sync reads it. So does a file that changed
on a server that fakes the permissions in its listing, such as ProFTPD with
`DirFakeMode`.

### SFTP: "… files changed or were deleted while this sync ran, …"

These files changed during the read, or were deleted after the listing. They are
counted as `skipped`, and the next sync reads them or removes their documents.
The connector logs one of these sentences for each file, at the info level: "…
changed while it was downloaded.", "… was replaced while it was downloaded.", or
"… was deleted before it was downloaded." (or "while it was downloaded."). The
shipped worker does not print info lines, so its log does not show them by
default. If the same file shows this on every sync, one cause is a server that reports the
current time as the file's modification time. Ask the administrator about the
server.

### Scheduled syncs are not running

A background task system must be running. Check that your worker process
is active:

Without a worker, only manual triggers via CLI or API will work.
