# Brains

A Brain is a workspace knowledge base with immutable revisions, a searchable note
index and human-reviewed writes. The database stores its canonical files; GitHub,
reader snapshots and vector indexes are optional copies.

## Create, import and browse

Open **Console → Brains → Create**. Start with the bundled template, upload a
UTF-8 ZIP, or import a public `https://github.com/owner/repository` URL. Imports
accept up to 50 MB compressed and expanded, 20,000 files, 1 MB per note and 4 MB
per `raw/` file. Archives cannot contain symlinks, encrypted entries or unsafe
paths. GitHub archives may wrap the tree in a single directory.

The contract requires `CLAUDE.md` and content under `identity/`, `knowledge/`
and `lenses/`. Contract 1.0 imports retain their bytes; legacy index descriptions
are harvested once. The bundled contract is 1.1. An old contract produces a
notice rather than an automatic rewrite.

The Files view shows notes, tiers and stale project notices. Revisions records
every committed file change and its diff. Workspace owners/admins can edit files
directly, change settings and manage grants. Workspace members can browse all
tiers and submit ordinary source proposals. Export ZIP downloads the complete
private tree; keep exported archives appropriately restricted.

Brain slugs are lowercase and unique in a workspace regardless of case; a
mixed-case slug from before 2.0 still answers when typed in another case. Only a
workspace owner can delete a brain (Brain settings → Delete this brain, typing
its slug): its notes, revisions, grants, feeds, GitHub sync settings, search
chunks and the API tokens bound to it go with it. Plugin-owned brains are
removed by their plugin.

Ask brain (the reader) works on the notes your tier can see. A lens may set
`visibility-ceiling` in its frontmatter: with that lens the reader sees only
notes at or below the ceiling (an audience-facing lens with `public` never draws
on agents-only or private notes), and the result reports the tier used. A cited
note the reader never opened is left out of the sources and named in a note.

## Access and visibility

The tiers are `public`, `agents-only`, then `private`. Explicit frontmatter wins
over folder defaults. Malformed frontmatter fails closed to private.
`(agents-only: …)` spans disappear from public views. An inaccessible note and a
missing note return the same unknown response. Raw archives require a visible
note linking to them.

Scripts, plugins, agents and API tokens need an explicit Brain grant. A Unified
API token also needs `brain:read` or `brain:write` permissions; a token binding
restricts it to one Brain, and its tier is intersected with the grant ceiling.
A plugin has implicit access to Brains it owns. Console membership does not
create grants for outside callers. See [API and MCP](api.md) and the
[plugin SDK](plugins.md).

## Feed inbox

Every outside write starts as a source in the inbox. The feeder uses an
AIProvider profile to propose changes, and the validator checks them against the
Brain contract. It never commits directly. Owners/admins review the diff and
approve or reject; ordinary proposals can also be edited before review.

Each source gets a source id, which its notes cite as `source` and its approval
revision names (`Feed: <id>`). The id is `<kind>-<YYYY-MM>-<slug>`, with the slug
taken from the title, else the URL path, else the source's first few words, else
`source`. When the Brain already has that id, the start of the new feed's id is
appended (`thought-2026-11-remember-this-3f9a`), so a new source never takes an id
the Brain already has.

Server-side URL intake allows HTTP/HTTPS, applies network policy to each redirect,
and limits downloads to 15 seconds, three redirects and 512 KB. Paste text when a
site cannot be fetched. The feeder retries transient failures up to three times.
An optional daily cost cap is configured per Brain. While any call today has an
unknown cost, including one that is still running, a capped Brain refuses the
next call instead of assuming it was free: a source captured meanwhile fails
with a `daily_cap` error and needs **Retry** once the running call has finished.

Approval creates one revision atomically. Repeated approval cannot apply it twice.
Disjoint concurrent changes are preserved; overlapping changes require a fresh
review. Restored feeds never silently restart paid extraction.

## Reader and semantic recall

**Ask brain** queues a worker job and shows the result asynchronously. The reader
uses the selected AIProvider and only a derived snapshot of the caller's tier.
Its Read/Grep/Glob tools are confined to that snapshot. Returned note IDs must
have been read and must still be visible; a changed head or revoked access
discards the answer. REST/MCP use the same reader under a bounded call budget.

Semantic search is optional. Bind a dedicated vector collection in Brain
settings to enable `brain.search` and reader preselection. All tiers are indexed
in that collection, so its store and embedding provider must be trusted with
private text. Anyone with direct collection access can inspect those chunks.
Brain queries recheck hits against canonical content and current grants.
Saving settings rebuilds a missing index; backup restore resets derived index
state without starting embedding calls automatically.

## GitHub sync

GitHub sync uses HTTP only. There is no checkout, git binary or shared canonical
directory. Use a dedicated private repository and an existing branch. Create a
workspace **Secret** containing a GitHub token with repository Contents read/write
permission, then choose it under **Brain → GitHub sync**. Secret values are never
shown on that page. Protected branches may refuse exports.

**Pull for review** downloads the exact remote commit as a bounded ZIP and
creates a `sync` feed. It compares against the last accepted/exported remote
manifest, preserving unrelated local edits. Review changes in the existing inbox.
Sync is an exact repository import: unlike AI proposals, it can modify identity,
the contract and visibility, and delete files. Its validator checks paths, sizes,
contract structure, duplicate entity IDs, frontmatter and closed visibility
spans. Owners/admins alone can edit or approve these proposals. No model rewrites
their contents. Rejected changes must be corrected on GitHub before another
proposal for the same baseline and commit is created.

Optional polling checks every five minutes through the existing worker heartbeat.
It only pulls; it never approves or exports. Failed jobs require attention on the
sync page. There is no webhook endpoint to configure.

**Queue export** captures the current immutable revision and replaces the entire
destination branch tree, including deletions, with all tiers and the contract.
The form requires acknowledgement of this private-data copy. For the first export,
review the destination branch and paste its full current commit SHA. Subsequent
exports use the accepted baseline. Unreviewed remote changes cause a conflict.
The API creates a tree and commit with that parent, then updates the reference
without force; a concurrent branch update is refused. GitHub documents this
behavior in [Update a reference](https://docs.github.com/en/rest/git/refs#update-a-reference).

Jobs use Workers and have a bounded HTTP budget. Refresh the page for status.
After an interrupted export, pull to establish whether GitHub accepted the commit
before retrying. Backups include configuration and the remote baseline, omit
transient jobs, and disable polling on restore. Verify the Secret and pull before
resuming. A Brain excluded from backups excludes its sync configuration too.

## Migrate from BrainOutside

1. Export the old Brain as a ZIP, or import its public GitHub repository. For a
   private repository, download its archive yourself and upload the ZIP. Keep the
   old deployment available until the new import has been checked.
2. Compare file counts, representative private/public notes, raw links and the
   imported revision. Review the contract-version notice. The old filesystem and
   Git history are not imported as PyRun revision history; the archive is revision 1.
3. Configure an AIProvider for feeder/reader calls, then create a Unified API token
   bound to this Brain. Add its Brain grant with the intended tier. Proposals need
   `brain:write`, `can_propose`, and agents-only or higher access.
4. Replace the connector URL with this instance's `/mcp`, and replace the old
   credential with the new token. For URL-only clients use an explicitly enabled,
   expiring `/mcp/k/<url-token>/` credential. Discover tools again: PyRun MCP
   names are `brain-get-note`, `brain-get-index`, etc. REST uses `brain.get-note`,
   `brain.get-index`, etc.; update hard-coded calls to match the chosen transport.
   Note semantics are retained.
5. Verify discovery, one public read, one forbidden read, a proposal appearing in
   the inbox, and revocation. Configure GitHub sync separately if wanted. No old
   consumers, PATs, deploy keys or login sessions are transferred automatically.

SQLite supports a personal Brain. PostgreSQL is recommended for more than five
Brains or multiple workers. Keep regular database/portable backups; deleting
reader caches or vector indexes cannot replace backing up canonical revisions.
