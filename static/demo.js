// Live demo mode. When the page is opened without its Python server (on GitHub Pages, as a file on disk, or with
// ?demo in the address), this answers the app's /api requests from memory, so everything works for one session.
// Nothing is saved: closing or reloading the tab starts afresh. On the real server this file does nothing.
(() => {
  const demo = /\.github\.io$/.test(location.hostname) || location.protocol === "file:" || new URLSearchParams(location.search).has("demo");
  if (!demo) return;
  window.EXAM_DEMO = true;

  const stores = { exams: new Map(), plans: new Map() };  // lesson plans live separately, as on the server
  const exams = stores.exams;                        // uid -> { owner, exam, updated_at }
  const settings = { pin_set: true, has_logo: false };  // the app fills in its built-in defaults
  let clock = 0;
  const now = () => new Date(Date.now() + clock++).toISOString();  // strictly increasing, for "most recent first"
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  const summary = (uid, r) => ({
    uid, owner: r.owner, learning_area: r.exam.learning_area || "", subject: r.exam.subject || "", title: r.exam.title || "",
    shared: r.exam.shared ? 1 : 0, updated_at: r.updated_at, updated_by: r.updated_by || "", topic: r.exam.unit ?? null, assessment_type: r.exam.assessment_type ?? null,
    year_level: r.exam.year_level ?? null, task: r.exam.task ?? null, semester: r.exam.semester ?? null, year: r.exam.year ?? null,
    total_marks: r.exam.total_marks ?? null,
  });

  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, opts = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const at = url.pathname.indexOf("/api/");
    if (at < 0) return realFetch(input, opts);
    const [area, id, sub] = url.pathname.slice(at + 5).split("/");
    const method = (opts.method || "GET").toUpperCase();

    if (area === "settings") {
      if (id === "logo") {
        if (window.demoLogoUrl) URL.revokeObjectURL(window.demoLogoUrl);
        window.demoLogoUrl = method === "PUT" ? URL.createObjectURL(opts.body) : "";
        settings.has_logo = method === "PUT";
        return json({ ok: true });
      }
      if (id === "unlock" || id === "pin") return json({ ok: true });
      if (method === "PUT") Object.assign(settings, JSON.parse(opts.body));
      return json(settings);
    }

    const owner = (url.searchParams.get("owner") || "").trim().toUpperCase();
    if (!/^[A-Z]{3}$/.test(owner)) return json({ error: "Enter your three-letter staff code (for example ABC) to use your exams." }, 400);
    const newestFirst = rows => rows.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    if (area === "exams" && !id) {
      return json(newestFirst([...exams].filter(([, r]) => r.owner === owner).map(([uid, r]) => summary(uid, r))));
    }
    if (area === "shelf") {
      const learningArea = url.searchParams.get("learning_area");
      return json(newestFirst([...exams].filter(([, r]) => r.exam.shared && (!learningArea || r.exam.learning_area === learningArea))
        .map(([uid, r]) => summary(uid, r))));
    }
    if (area === "plans" && !id) {
      return json(newestFirst([...stores.plans].filter(([, r]) => r.owner === owner).map(([uid, r]) => ({ ...summary(uid, r),
        class_code: r.exam.class_code ?? null, lesson_date: r.exam.lesson_date ?? null, topic: r.exam.topic ?? null }))));
    }
    if (area === "exams" || area === "plans") {
      const exams = stores[area];
      const r = exams.get(id), notFound = json({ error: "Exam not found." }, 404);
      const visible = r && (r.owner === owner || r.exam.shared);
      if (method === "GET" && sub === "version") return visible ? json({ updated_at: r.updated_at, updated_by: r.updated_by || "" }) : notFound;
      if (method === "GET") return visible ? json({ ...summary(id, r), exam: structuredClone(r.exam) }) : notFound;
      if (method === "PUT") {
        if (r && r.owner !== owner) return notFound;
        const base = url.searchParams.get("base");  // as server.py: a save from an older version is refused
        if (base && r && r.updated_at !== base) {
          return json({ error: "This exam was changed somewhere else since you opened it.", updated_at: r.updated_at, updated_by: r.updated_by || "" }, 409);
        }
        const updated_at = now();
        exams.set(id, { owner, exam: JSON.parse(opts.body), updated_at, updated_by: url.searchParams.get("by") || "" });
        return json({ ok: true, updated_at });
      }
      if (method === "DELETE") {
        if (!r || r.owner !== owner) return notFound;
        exams.delete(id);
        return json({ ok: true });
      }
    }
    return json({ error: "Not found." }, 404);
  };

  // Closing the tab loses everything, so ask first once there is something to lose.
  addEventListener("beforeunload", e => { if (exams.size || stores.plans.size) { e.preventDefault(); e.returnValue = ""; } });
})();
