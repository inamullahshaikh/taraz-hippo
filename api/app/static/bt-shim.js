/* BOMTree on Railway. The page was written for claude.ai, where window.claude hands it the viewer's connectors.
   Here this stands in for it: the BOMTree connector's calls go straight to this server, which answers them from Postgres.
   Odoo, Drive, Blue and pictures are not connected here yet; the page already copes when they are missing. */
(() => {
  async function post(path, body) {
    const r = await fetch(path, {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) location.href = "/signin?next=" + encodeURIComponent(location.pathname);
    if (!r.ok) {
      const d = j.detail || {};
      throw {code: d.code || "server_error", message: d.message || (typeof d === "string" ? d : "BOMTree server error " + r.status)};
    }
    return j;
  }
  const mcp = {
    async callTool(server, tool, input) {
      if (server === "BOMTree") return {payload: tool === "list_tabs" ? {} : await post("/api/v1/sheets/" + tool, input)};
      throw {code: "not_here", message: server + " is not connected in BOMTree on Railway yet"};
    },
  };
  let meP = null;
  const me = () => meP || (meP = fetch("/api/v1/me", {credentials: "same-origin"}).then(r => r.ok ? r.json() : null).catch(() => null));
  const user = {
    async me() { const m = await me(); return {id: m ? String(m.user.id) : "", name: m ? m.user.name : ""}; },
    async id() { const m = await me(); return m ? String(m.user.id) : ""; },
    isOwner: () => false, canEdit: () => false, can: () => null,
    async profiles() { return {}; }, async search() { return []; },
  };
  // Local testing only: localStorage "bt.dev" = "live" (org member) or "copy" (no connector) gives the page a store
  // like claude.ai's, kept by the local server (/dev/store exists only with DEV_LOGIN).
  const dev = localStorage.getItem("bt.dev");
  const devPost = b => fetch("/dev/store", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(b)}).then(r => r.json());
  const snap = (id, d) => ({id, exists: d != null, data: () => (d == null ? undefined : d), metadata: {}});
  const doc = path => ({id: path.split("/").pop(), path,
    get: async () => snap(path.split("/").pop(), (await devPost({op: "get", path})).data),
    set: async data => { await devPost({op: "set", path, data}); }, delete: async () => { await devPost({op: "delete", path}); }});
  const coll = (path, by) => ({path, doc: id => doc(path + "/" + (id || crypto.randomUUID())), orderBy: f => coll(path, f),
    add: async d => { const r = doc(path + "/" + crypto.randomUUID()); await r.set(d); return r; },
    get: async () => { const docs = (await devPost({op: "list", path})).docs.map(x => snap(x.id, x.data));
                       if (by) docs.sort((a, b) => a.data()[by] - b.data()[by]); return {docs, size: docs.length, empty: !docs.length}; }});
  const devdb = dev && {doc, collection: coll};
  window.claude = {use: async name => name === "mcp" ? (dev === "copy" ? null : mcp) : name === "user" ? user : name === "db" ? devdb || null : null};
})();
