/* NitoCAD hero: a blueprint that draws itself stroke by stroke, then lifts into a solid.
   Sixteen swappable scenes: two complete engines (inline-4, single-cylinder), crankshaft, camshaft and machined parts (gear, bracket, flange, shaft, bearing, pulley, piston, con-rod, brake disc, spring, bolt, enclosure). Uses the same three.js build the page already
   loads through its import map, so it adds no new network dependency.
   Pauses when off-screen or in a background tab, and respects prefers-reduced-motion. */
import * as THREE from "three";

const stage = document.getElementById("bpStage");
const host = stage && stage.querySelector(".bp-canvas");
if (stage && host) boot();

function boot() {
  const reduce = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "low-power" });
  } catch (err) {
    stage.classList.add("bp-off");
    return;
  }
  renderer.setClearColor(0x000000, 0);
  renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.15; renderer.localClippingEnabled = true;
  host.appendChild(renderer.domElement);
  stage.classList.add("bp-live");

  const COL = { line: 0xcfd0d4, lineSolid: 0x3a3a3e, dim: 0xc9c9ce, grid: 0x8a8a90 };
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, 1.25, 0.1, 400);
  const envTex = (() => { const c = document.createElement("canvas"); c.width = 256; c.height = 128; const g = c.getContext("2d"), gr = g.createLinearGradient(0, 0, 0, 128); gr.addColorStop(0, "#7fa8e6"); gr.addColorStop(0.46, "#e9eef5"); gr.addColorStop(0.5, "#8a8f98"); gr.addColorStop(1, "#2b313b"); g.fillStyle = gr; g.fillRect(0, 0, 256, 128); g.fillStyle = "#fff"; g.fillRect(150, 14, 56, 26); const t = new THREE.CanvasTexture(c); t.mapping = THREE.EquirectangularReflectionMapping; t.colorSpace = THREE.SRGBColorSpace; return t; })();
  scene.environment = envTex; scene.environmentIntensity = 0.7;
  scene.add(new THREE.HemisphereLight(0xffffff, 0x1b3358, 0.55));
  const rim = new THREE.DirectionalLight(0x9fc3ff, 0.6); rim.position.set(-8, 4, -6); scene.add(rim);
  const sun = new THREE.DirectionalLight(0xfff1dc, 2.2);
  sun.position.set(6, 10, 8);
  scene.add(sun);

  /* ---------- geometry helpers ---------- */
  const xf = (arr, m) => {
    const out = new Float32Array(arr.length), v = new THREE.Vector3();
    for (let i = 0; i < arr.length; i += 3) {
      v.set(arr[i], arr[i + 1], arr[i + 2]).applyMatrix4(m);
      out[i] = v.x; out[i + 1] = v.y; out[i + 2] = v.z;
    }
    return out;
  };
  const cat = (...as) => {
    const out = new Float32Array(as.reduce((s, a) => s + a.length, 0));
    let o = 0;
    for (const a of as) { out.set(a, o); o += a.length; }
    return out;
  };
  const edgeSeg = (geo, m, ang) => {
    const e = new THREE.EdgesGeometry(geo, ang);
    const a = e.attributes.position.array.slice();
    e.dispose();
    return xf(a, m);
  };
  const polyShape = (pts) => {
    const s = new THREE.Shape();
    pts.forEach((p, i) => (i ? s.lineTo(p[0], p[1]) : s.moveTo(p[0], p[1])));
    s.closePath();
    return s;
  };
  const rectPts = (x0, y0, x1, y1) => [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
  const rectPath = (x0, y0, x1, y1) => polyShape(rectPts(x0, y0, x1, y1));
  const extrude = (shape, depth) =>
    new THREE.ExtrudeGeometry(shape, { depth, bevelEnabled: false, steps: 1 });

  const mat = (color, max = 1, o = {}) => {
    const key = o._share ? [color, max, o.r, o.m, o.em, o.flat].join("|") : null;
    if (key && MATC.has(key)) return MATC.get(key);
    const gl = GLOSSY.has(color);
    const m = new THREE.MeshStandardMaterial({
      color, side: THREE.DoubleSide, transparent: true, opacity: 0,
      roughness: gl ? 0.08 : (o.r == null ? 0.72 : o.r), metalness: gl ? 0.4 : (o.m || 0),
      vertexColors: !!(o.paint || o.vc), map: o.map || null, flatShading: !!o.flat, emissive: o.em || 0x000000,
      polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1
    });
    m.envMapIntensity = gl ? 1.6 : 1;
    m.userData.max = max;
    if (key) MATC.set(key, m);
    return m;
  };

  function mk(name, geo, color, o = {}) {
    if (o.paint) paintGeo(geo, o.paint);
    const T = o.tex === undefined ? TEXBY.get(color) : o.tex;
    const mesh = new THREE.Mesh(geo, mat(color, o.max == null ? 1 : o.max, Object.assign({}, o, { _share: !(T || o.paint || o.vc || o.map || o.anim) })));
    if (o.anim) mesh.userData.anim = o.anim;
    if (T) {
      const pr = geo.parameters, bx = pr && pr.width != null, m = mesh.material;
      m.map = rep(T.t, bx ? Math.max(pr.width, pr.depth || 0) / T.w : 1 / T.w, bx ? Math.max(pr.height, pr.depth || 0) / T.h : 1 / T.h);
      if (T.bump) { m.bumpMap = m.map; m.bumpScale = 1.5; }
    }
    if (o.dress) mesh.userData.dress = true;
    if (o.pos) mesh.position.set(o.pos[0], o.pos[1], o.pos[2]);
    if (o.rot) mesh.rotation.set(o.rot[0], o.rot[1], o.rot[2]);
    if (o.scl) mesh.scale.set(o.scl[0], o.scl[1], o.scl[2]);
    mesh.updateMatrix();
    let seg = o.seg ? o.seg(mesh.matrix) : o.dress ? new Float32Array(0) : edgeSeg(geo, mesh.matrix, o.angle == null ? 28 : o.angle);
    if (o.extra) seg = cat(seg, o.extra);
    return { name, mesh, seg, dress: !!o.dress };
  }

  // Station rings plus longitudinal lines, the way a lofted fuselage is drawn.
  function latheLines(ctrl, sm, nLong) {
    const out = [], N = 28, TAU = Math.PI * 2;
    const push = (ax, ay, az, bx, by, bz) => out.push(ax, ay, az, bx, by, bz);
    for (const c of ctrl) {
      if (c[0] < 0.03) continue;
      for (let k = 0; k < N; k++) {
        const a0 = (k / N) * TAU, a1 = ((k + 1) / N) * TAU;
        push(c[0] * Math.cos(a0), c[1], c[0] * Math.sin(a0), c[0] * Math.cos(a1), c[1], c[0] * Math.sin(a1));
      }
    }
    for (let j = 0; j < nLong; j++) {
      const a = (j / nLong) * TAU, ca = Math.cos(a), sa = Math.sin(a);
      for (let i = 0; i < sm.length - 1; i++) {
        push(sm[i].x * ca, sm[i].y, sm[i].x * sa, sm[i + 1].x * ca, sm[i + 1].y, sm[i + 1].x * sa);
      }
    }
    return new Float32Array(out);
  }
  function lathePart(name, ctrl, color, o = {}) {
    const sm = new THREE.SplineCurve(ctrl.map((c) => new THREE.Vector2(c[0], c[1])))
      .getPoints(o.res || 36).map((p) => new THREE.Vector2(Math.max(0, p.x), p.y));
    const geo = new THREE.LatheGeometry(sm, o.segs || 32);
    return mk(name, geo, color, Object.assign({}, o, { seg: (m) => xf(latheLines(ctrl, sm, o.long || 10), m) }));
  }

  // Engineering-style dimension line with end ticks.
  function dim(a, b, t, tick) {
    tick = tick || 0.32;
    const p = (v, s) => [v[0] + t[0] * tick * s, v[1] + t[1] * tick * s, v[2] + t[2] * tick * s];
    return new Float32Array([].concat(a, b, p(a, -1), p(a, 1), p(b, -1), p(b, 1)));
  }

  /* ---------- shared helpers for the detailed scenes ---------- */
  const lerpAt = (pts, k) => {
    // pts: [[t, v], ...] sorted by t. Piecewise-linear lookup.
    if (k <= pts[0][0]) return pts[0][1];
    for (let i = 1; i < pts.length; i++) {
      if (k <= pts[i][0]) {
        const a = pts[i - 1], b = pts[i];
        return a[1] + (b[1] - a[1]) * ((k - a[0]) / (b[0] - a[0] || 1));
      }
    }
    return pts[pts.length - 1][1];
  };

  // Many small quads (windows) merged into one mesh. rects: [cx, cy, cz, w, h, nx] with the
  // quad lying in a plane of constant z (nx = 0) or constant x (nx = 1), facing sign s.
  function quadBatch(rects) {
    const pos = [], idx = [], out = [];
    rects.forEach((r, i) => {
      const [cx, cy, cz, w, h, axis] = r;
      let a, b, c, d;
      if (axis === 0) {
        a = [cx - w / 2, cy - h / 2, cz]; b = [cx + w / 2, cy - h / 2, cz];
        c = [cx + w / 2, cy + h / 2, cz]; d = [cx - w / 2, cy + h / 2, cz];
      } else {
        a = [cx, cy - h / 2, cz - w / 2]; b = [cx, cy - h / 2, cz + w / 2];
        c = [cx, cy + h / 2, cz + w / 2]; d = [cx, cy + h / 2, cz - w / 2];
      }
      pos.push(...a, ...b, ...c, ...d);
      const o = i * 4;
      idx.push(o, o + 1, o + 2, o, o + 2, o + 3);
      out.push(...a, ...b, ...b, ...c, ...c, ...d, ...d, ...a);
    });
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(pos), 3));
    g.setIndex(idx);
    g.computeVertexNormals();
    return { geo: g, seg: new Float32Array(out) };
  }

  // A wing-like slab from a planform polygon [x, z] (z = span). Thickness hangs below the plane.
  function planform(pts, thick) {
    return extrude(polyShape(pts), thick);
  }

  /* ---------- realism helpers: textures, painted vertices, lofted airfoils, merged boxes ---------- */
  const TEXBY = new Map(), GLOSSY = new Set(), MATC = new Map();
  const rnd = (() => { let s = 11; return () => (s = (s * 16807) % 2147483647) / 2147483647; })();
  const mkTex = (w, h, draw) => {
    const c = document.createElement("canvas"); c.width = w; c.height = h; draw(c.getContext("2d"), w, h);
    const t = new THREE.CanvasTexture(c); t.wrapS = t.wrapT = THREE.RepeatWrapping; t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 4;
    return t;
  };
  const rep = (t, u, v) => { const c = t.clone(); c.needsUpdate = true; c.repeat.set(u, v); return c; };
  const BLOB = mkTex(128, 128, (g, w, h) => {
    const r = g.createRadialGradient(64, 64, 4, 64, 64, 62);
    r.addColorStop(0, "rgba(0,0,0,.9)"); r.addColorStop(0.55, "rgba(0,0,0,.55)"); r.addColorStop(1, "rgba(0,0,0,0)");
    g.fillStyle = r; g.fillRect(0, 0, w, h);
  });
  BLOB.wrapS = BLOB.wrapT = THREE.ClampToEdgeWrapping;
  const paintGeo = (geo, fn) => {
    const p = geo.attributes.position, c = new Float32Array(p.count * 3), k = new THREE.Color();
    for (let i = 0; i < p.count; i++) { k.set(fn(p.getX(i), p.getY(i), p.getZ(i))); c[i * 3] = k.r; c[i * 3 + 1] = k.g; c[i * 3 + 2] = k.b; }
    geo.setAttribute("color", new THREE.BufferAttribute(c, 3));
  };
  // Many boxes merged into one geometry. list: [cx, cy, cz, w, h, d]
  function boxes(list) {
    const pos = [], nor = [], uv = [], idx = [];
    list.forEach(([cx, cy, cz, w, h, d]) => {
      const g = new THREE.BoxGeometry(w, h, d), o = pos.length / 3, p = g.attributes.position.array;
      for (let i = 0; i < p.length; i += 3) pos.push(p[i] + cx, p[i + 1] + cy, p[i + 2] + cz);
      nor.push(...g.attributes.normal.array); uv.push(...g.attributes.uv.array);
      g.index.array.forEach((i) => idx.push(i + o));
      g.dispose();
    });
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    g.setAttribute("normal", new THREE.Float32BufferAttribute(nor, 3));
    g.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2));
    g.setIndex(idx);
    return g;
  }
  // Oval windows lying in planes of constant z. list: [cx, cy, cz, w, h]
  function ovals(list) {
    const pos = [], idx = [], out = [], N = 12;
    list.forEach(([cx, cy, cz, w, h]) => {
      const b = pos.length / 3;
      pos.push(cx, cy, cz);
      for (let k = 0; k < N; k++) {
        const a = (k / N) * 6.2832;
        pos.push(cx + Math.cos(a) * w / 2, cy + Math.sin(a) * h / 2, cz);
        idx.push(b, b + 1 + k, b + 1 + ((k + 1) % N));
      }
      for (let k = 0; k < N; k++) {
        const A = (b + 1 + k) * 3, B = (b + 1 + ((k + 1) % N)) * 3;
        out.push(pos[A], pos[A + 1], pos[A + 2], pos[B], pos[B + 1], pos[B + 2]);
      }
    });
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    g.setIndex(idx); g.computeVertexNormals();
    return { geo: g, seg: new Float32Array(out) };
  }
  // Lofted airfoil surface. secs: [{ le, te, t, up }] with t = max thickness in metres.
  const ytf = (s) => 0.2969 * Math.sqrt(s) - 0.126 * s - 0.3516 * s * s + 0.2843 * s ** 3 - 0.1036 * s ** 4;
  function loft(secs, col, lines, n = 14) {
    const pos = [], rgb = [], idx = [], out = [], M = 2 * n, k = new THREE.Color(), seq = [];
    for (let i = 0; i <= n; i++) seq.push([i, 1]);
    for (let i = n - 1; i > 0; i--) seq.push([i, -1]);
    secs.forEach((sc) => {
      const c = [sc.te[0] - sc.le[0], sc.te[1] - sc.le[1], sc.te[2] - sc.le[2]];
      seq.forEach(([i, sg]) => {
        const s = (1 - Math.cos(Math.PI * i / n)) / 2, h = 5.05 * sc.t * ytf(s) * sg;
        const p = [sc.le[0] + c[0] * s + sc.up[0] * h, sc.le[1] + c[1] * s + sc.up[1] * h, sc.le[2] + c[2] * s + sc.up[2] * h];
        pos.push(p[0], p[1], p[2]);
        k.set(col(p[0], p[1], p[2], sg)); rgb.push(k.r, k.g, k.b);
      });
    });
    for (let a = 0; a < secs.length - 1; a++) for (let j = 0; j < M; j++) {
      const j2 = (j + 1) % M, p = a * M + j, q = a * M + j2, r = (a + 1) * M + j, s2 = (a + 1) * M + j2;
      idx.push(p, q, r, q, s2, r);
    }
    const L = (secs.length - 1) * M;
    for (let j = 1; j < M - 1; j++) idx.push(L, L + j, L + j + 1);
    const ln = (A, B) => out.push(pos[A * 3], pos[A * 3 + 1], pos[A * 3 + 2], pos[B * 3], pos[B * 3 + 1], pos[B * 3 + 2]);
    secs.forEach((_, a) => { for (let j = 0; j < M; j++) ln(a * M + j, a * M + ((j + 1) % M)); });
    lines.forEach((i) => { for (let a = 0; a < secs.length - 1; a++) ln(a * M + i, (a + 1) * M + i); });
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    g.setAttribute("color", new THREE.Float32BufferAttribute(rgb, 3));
    g.setIndex(idx); g.computeVertexNormals();
    return { geo: g, seg: new Float32Array(out) };
  }

  function fanGeo() {
    const pos = [], idx = [];
    for (let k = 0; k < 18; k++) {
      const a = k * 6.2832 / 18, b = pos.length / 3;
      [[0.25, a - 0.06], [0.25, a + 0.06], [1.38, a + 0.18], [1.38, a + 0.04]].forEach(([r, q]) => pos.push(0, r * Math.cos(q), r * Math.sin(q)));
      idx.push(b, b + 1, b + 2, b, b + 2, b + 3);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3)); g.setIndex(idx); g.computeVertexNormals();
    return g;
  }

  /* ---------- machined-part scenes (1 unit = 1 mm) ---------- */
  // Closed ring as a hole path. Kept as a 32-gon so the wall facets stay under the edge threshold.
  const ring = (cx, cy, r, n) => {
    const a = [];
    for (let i = 0; i < n; i++) { const t = (i / n) * Math.PI * 2; a.push(new THREE.Vector2(cx + r * Math.cos(t), cy + r * Math.sin(t))); }
    return new THREE.Path(a);
  };
  const circY = (r, y, n) => {
    const o = [];
    for (let i = 0; i < n; i++) {
      const a = (i / n) * Math.PI * 2, b = ((i + 1) / n) * Math.PI * 2;
      o.push(r * Math.cos(a), y, r * Math.sin(a), r * Math.cos(b), y, r * Math.sin(b));
    }
    return new Float32Array(o);
  };
  const STEEL = { r: 0.45, m: 0.35 };

  /* Spur gear: module 2, 20 teeth, 20 degree pressure angle, 10 mm thick, 5 mm bore. True involute flanks. */
  function buildGear(cut) {
    TEXBY.clear(); GLOSSY.clear();
    const mod = 2, Z = 20, T = 10, BORE = 5, A = Math.PI / 9;
    const rp = (mod * Z) / 2, rb = rp * Math.cos(A), ro = rp + mod, rr = rp - 1.25 * mod;
    const inv = (a) => Math.tan(a) - a;
    const hb = Math.PI / (2 * Z) + inv(A);                       // half tooth angle at the base circle
    const ang = (r) => inv(Math.acos(Math.min(1, rb / r)));      // involute angle at radius r
    const pol = (r, a) => [r * Math.cos(a), r * Math.sin(a)];
    const step = (Math.PI * 2) / Z, NF = 7, pts = [];
    for (let i = 0; i < Z; i++) {
      const c = i * step;
      pts.push(pol(rr, c - hb));
      for (let k = 0; k <= NF; k++) { const r = rb + (ro - rb) * (k / NF); pts.push(pol(r, c - hb + ang(r))); }
      for (let k = NF; k >= 0; k--) { const r = rb + (ro - rb) * (k / NF); pts.push(pol(r, c + hb - ang(r))); }
      pts.push(pol(rr, c + hb));
      const a0 = c + hb, a1 = c + step - hb;
      for (let k = 1; k <= 3; k++) pts.push(pol(rr, a0 + (a1 - a0) * (k / 4)));
    }
    const shape = polyShape(pts);
    shape.holes.push(ring(0, 0, BORE / 2, 32));
    const extra = cat(circY(rp, T + 0.04, 72), new Float32Array([0, -3, 0, 0, T + 4, 0]));   // pitch circle and axis
    const P = [mk("Spur gear", extrude(shape, T), 0xb9bfc8, Object.assign({ rot: [-Math.PI / 2, 0, 0], extra }, STEEL))];
    return {
      parts: P,
      clip: cut ? new THREE.Plane(new THREE.Vector3(0, 0, -1), 0) : null,
      focus: null,
      gridStep: 5,
      dims: (box) => cat(
        dim([box.min.x, 0, box.max.z + 5], [box.max.x, 0, box.max.z + 5], [0, 0, 1], 1.6),
        dim([box.max.x + 5, 0, 0], [box.max.x + 5, T, 0], [1, 0, 0], 1.6)),
      groundOffset: 0.0
    };
  }

  /* L-bracket: 50 wide, 60 tall, 40 deep, 3 mm thick, 3 mm inside bend radius, 2 holes per leg. */
  function buildBracket(cut) {
    TEXBY.clear(); GLOSSY.clear();
    const W = 50, D = 40, H = 60, t = 3, ri = 3, ro = ri + t, hr = 2.75, N = 10, MET = 0xc3c8d0, P = [];
    const base = polyShape(rectPts(-W / 2, ro, W / 2, D));
    [-14, 14].forEach((x) => base.holes.push(ring(x, 26, hr, 32)));
    P.push(mk("Base leg", extrude(base, t), MET, Object.assign({ rot: [Math.PI / 2, 0, 0], pos: [0, t, -D / 2] }, STEEL)));
    const up = polyShape(rectPts(-W / 2, ro, W / 2, H));
    [-14, 14].forEach((x) => up.holes.push(ring(x, 40, hr, 32)));
    P.push(mk("Upright leg", extrude(up, t), MET, Object.assign({ pos: [0, 0, -D / 2] }, STEEL)));
    const bp = [];
    for (let k = 0; k <= N; k++) { const a = Math.PI + (k / N) * (Math.PI / 2); bp.push([ro + ro * Math.cos(a), ro + ro * Math.sin(a)]); }
    for (let k = N; k >= 0; k--) { const a = Math.PI + (k / N) * (Math.PI / 2); bp.push([ro + ri * Math.cos(a), ro + ri * Math.sin(a)]); }
    P.push(mk("Bend", extrude(polyShape(bp), W), MET, Object.assign({ rot: [0, -Math.PI / 2, 0], pos: [W / 2, 0, -D / 2] }, STEEL)));
    return {
      parts: P,
      clip: cut ? new THREE.Plane(new THREE.Vector3(-1, 0, 0), 0) : null,
      focus: null,
      gridStep: 5,
      dims: (box) => cat(
        dim([box.min.x, 0, box.max.z + 6], [box.max.x, 0, box.max.z + 6], [0, 0, 1], 1.8),
        dim([box.max.x + 6, 0, box.min.z], [box.max.x + 6, H, box.min.z], [1, 0, 0], 1.8),
        dim([box.max.x + 6, 0, box.min.z], [box.max.x + 6, 0, box.max.z], [1, 0, 0], 1.8)),
      groundOffset: 0.0
    };
  }

  /* ---------- more machined parts (1 unit = 1 mm) ---------- */
  const TAU = Math.PI * 2;
  const ngon = (r, n, a0) => {
    const p = [];
    for (let i = 0; i < n; i++) { const a = (a0 || 0) + (i / n) * TAU; p.push([r * Math.cos(a), r * Math.sin(a)]); }
    return p;
  };
  const discShape = (r, n) => polyShape(ngon(r, n || 96));
  const pathOf = (pts) => new THREE.Path(pts.map((p) => new THREE.Vector2(p[0], p[1])));
  // Rounded rectangle outline, w x h, corner radius r, n segments per corner.
  const rrPts = (w, h, r, n) => {
    const out = [], cs = [[1, 1], [-1, 1], [-1, -1], [1, -1]];
    for (let k = 0; k < 4; k++) {
      const cx = cs[k][0] * (w / 2 - r), cy = cs[k][1] * (h / 2 - r);
      for (let j = 0; j <= n; j++) { const a = (k + j / n) * (Math.PI / 2); out.push([cx + r * Math.cos(a), cy + r * Math.sin(a)]); }
    }
    return out;
  };
  const hull = (pts) => {
    pts = pts.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const cr = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const lo = [], up = [];
    for (const p of pts) { while (lo.length >= 2 && cr(lo[lo.length - 2], lo[lo.length - 1], p) <= 0) lo.pop(); lo.push(p); }
    for (let i = pts.length - 1; i >= 0; i--) { const p = pts[i]; while (up.length >= 2 && cr(up[up.length - 2], up[up.length - 1], p) <= 0) up.pop(); up.push(p); }
    up.pop(); lo.pop();
    return lo.concat(up);
  };
  // Three great circles, so a ball reads as a ball in the line drawing.
  const sphereLines = (r) => {
    const o = [], N = 28;
    for (let i = 0; i < N; i++) {
      const a = (i / N) * TAU, b = ((i + 1) / N) * TAU, ca = r * Math.cos(a), sa = r * Math.sin(a), cb = r * Math.cos(b), sb = r * Math.sin(b);
      o.push(ca, sa, 0, cb, sb, 0, ca, 0, sa, cb, 0, sb, 0, ca, sa, 0, cb, sb);
    }
    return new Float32Array(o);
  };
  // Solid of revolution about the Y axis from a closed (radius, height) outline. Step corners and chamfers
  // show up as rings; straight lines down the outline at four angles give the drawn look.
  function revolve(name, prof, color, o) {
    o = o || {};
    const pr = prof.slice();
    if (pr[0][0] !== pr[pr.length - 1][0] || pr[0][1] !== pr[pr.length - 1][1]) pr.push(pr[0]);
    const geo = new THREE.LatheGeometry(pr.map((p) => new THREE.Vector2(p[0], p[1])), o.segs || 64);
    const lines = [];
    [0, Math.PI / 2, Math.PI, Math.PI * 1.5].forEach((a) => {
      const ca = Math.cos(a), sa = Math.sin(a);
      for (let i = 0; i < pr.length - 1; i++) {
        const p = pr[i], q = pr[i + 1];
        if (p[0] < 1e-6 && q[0] < 1e-6) continue;
        lines.push(p[0] * ca, p[1], p[0] * sa, q[0] * ca, q[1], q[0] * sa);
      }
    });
    const ln = new Float32Array(lines);
    return mk(name, geo, color, Object.assign({}, o, { seg: (m) => cat(edgeSeg(geo, m, 28), xf(ln, m)) }));
  }
  const FLAT = new THREE.Vector3(0, 0, -1);
  const halfCut = (cut) => (cut ? new THREE.Plane(FLAT.clone(), 0) : null);

  /* Round flange: 100 mm OD, 12 mm thick, 6 x 9 mm holes on a 75 mm circle, 30 mm bore, 50 mm hub 10 mm tall. */
  function buildFlange(cut) {
    TEXBY.clear(); GLOSSY.clear();
    const R = 50, T = 12, BORE = 15, PCD = 75, HR = 4.5, HUB = 25, HH = 10, MET = 0xb9bfc8;
    const plate = discShape(R, 96);
    plate.holes.push(ring(0, 0, BORE, 48));
    for (let i = 0; i < 6; i++) { const a = (i / 6) * TAU; plate.holes.push(ring((PCD / 2) * Math.cos(a), (PCD / 2) * Math.sin(a), HR, 32)); }
    const extra = cat(circY(PCD / 2, T + 0.04, 72), new Float32Array([0, -3, 0, 0, T + HH + 4, 0]));
    const P = [
      mk("Flange plate", extrude(plate, T), MET, Object.assign({ rot: [-Math.PI / 2, 0, 0], extra }, STEEL)),
      revolve("Hub", [[BORE, T], [HUB, T], [HUB, T + HH], [BORE, T + HH]], MET, Object.assign({ segs: 64 }, STEEL))
    ];
    return {
      parts: P, clip: halfCut(cut), focus: null, gridStep: 5,
      dims: (box) => cat(
        dim([box.min.x, 0, box.max.z + 6], [box.max.x, 0, box.max.z + 6], [0, 0, 1], 1.8),
        dim([box.max.x + 6, 0, 0], [box.max.x + 6, T + HH, 0], [1, 0, 0], 1.8)),
      groundOffset: 0.0
    };
  }

  /* Stepped shaft: 160 mm long, diameters 20 / 25 / 30 / 25 / 20, 1 mm end chamfers. Lies along X. */
  function buildShaft(cut) {
    TEXBY.clear(); GLOSSY.clear();
    const L = 160;
    const prof = [[0, 0], [9, 0], [10, 1], [10, 30], [12.5, 30], [12.5, 90], [15, 90], [15, 120], [12.5, 120], [12.5, 145], [10, 145], [10, 159], [9, L], [0, L]];
    const P = [revolve("Stepped shaft", prof, 0xb9bfc8, Object.assign({ segs: 64, rot: [0, 0, Math.PI / 2], pos: [L / 2, 0, 0], extra: new Float32Array([-L / 2 - 8, 0, 0, L / 2 + 8, 0, 0]) }, STEEL))];
    return {
      parts: P, clip: halfCut(cut), focus: null, gridStep: 5,
      dims: (box) => cat(
        dim([-L / 2, 0, box.max.z + 8], [L / 2, 0, box.max.z + 8], [0, 0, 1], 2),
        dim([-25, -15, box.max.z + 8], [-25, 15, box.max.z + 8], [1, 0, 0], 2)),
      groundOffset: 0.0
    };
  }

  /* Deep groove ball bearing 6205: 25 bore, 52 OD, 15 wide, nine 7.94 mm balls on a 38.5 mm pitch circle. */
  function buildBearing(cut) {
    TEXBY.clear(); GLOSSY.clear();
    const B = 15, RP = 19.25, RB = 3.97, RG = 4.05, MID = B / 2, MET = 0xb9bfc8;
    const arc = (sign, from, to, n) => {
      const out = [];
      for (let k = 0; k <= n; k++) { const d = from + (to - from) * (k / n); out.push([RP + sign * Math.sqrt(RG * RG - d * d), MID + d]); }
      return out;
    };
    const so = RP + Math.sqrt(RG * RG - 3.5 * 3.5), si = RP - Math.sqrt(RG * RG - 3.5 * 3.5);
    const outer = [[26, 0], [26, B], [so, B]].concat(arc(1, 3.5, -3.5, 10), [[so, 0]]);
    const inner = [[12.5, 0], [si, 0]].concat(arc(-1, -3.5, 3.5, 10), [[si, B], [12.5, B]]);
    const P = [
      revolve("Outer ring", outer, MET, Object.assign({ segs: 72 }, STEEL)),
      revolve("Inner ring", inner, MET, Object.assign({ segs: 72 }, STEEL))
    ];
    for (let k = 0; k < 9; k++) {
      const a = (k / 9) * TAU, bx = RP * Math.cos(a), bz = RP * Math.sin(a), sl = sphereLines(RB);
      P.push(mk("Ball " + (k + 1), new THREE.SphereGeometry(RB, 28, 18), 0xe3e6ea, { pos: [bx, MID, bz], r: 0.2, m: 0.6, seg: (m) => xf(sl, m) }));
    }
    return {
      parts: P, clip: halfCut(cut), focus: null, gridStep: 5,
      dims: (box) => cat(
        dim([box.min.x, 0, box.max.z + 5], [box.max.x, 0, box.max.z + 5], [0, 0, 1], 1.5),
        dim([box.max.x + 5, 0, 0], [box.max.x + 5, B, 0], [1, 0, 0], 1.5)),
      groundOffset: 0.0
    };
  }

  /* V-belt pulley: 120 mm OD, one 38 degree groove, 20 mm bore, 44 mm hub 40 mm long, 8 mm web. */
  function buildPulley(cut) {
    TEXBY.clear(); GLOSSY.clear();
    const prof = [[10, 0], [22, 0], [22, 16], [46, 16], [46, 8], [60, 8], [60, 12.56], [50, 16], [50, 24], [60, 27.44], [60, 32], [46, 32], [46, 24], [22, 24], [22, 40], [10, 40]];
    const extra = new Float32Array([0, -3, 0, 0, 44, 0]);
    const P = [revolve("V-belt pulley", prof, 0xb9bfc8, Object.assign({ segs: 96, extra }, STEEL))];
    return {
      parts: P, clip: halfCut(cut), focus: null, gridStep: 5,
      dims: (box) => cat(
        dim([box.min.x, 0, box.max.z + 6], [box.max.x, 0, box.max.z + 6], [0, 0, 1], 1.8),
        dim([box.max.x + 6, 0, 0], [box.max.x + 6, 40, 0], [1, 0, 0], 1.8)),
      groundOffset: 0.0
    };
  }

  /* Piston: 80 mm bore, 60 mm tall, three ring grooves, 22 mm wrist pin on two bosses. */
  function buildPiston(cut) {
    TEXBY.clear(); GLOSSY.clear();
    const prof = [[0, 60], [38.6, 60], [39.6, 59], [39.6, 52], [36.5, 52], [36.5, 50], [39.6, 50], [39.6, 46], [36.5, 46], [36.5, 44], [39.6, 44], [39.6, 40],
      [36.5, 40], [36.5, 36], [39.9, 36], [39.9, 1], [38.9, 0], [33, 0], [33, 52], [0, 52]];
    const P = [revolve("Piston", prof, 0xcfd3d9, Object.assign({ segs: 96, extra: new Float32Array([0, -3, 0, 0, 64, 0]) }, STEEL))];
    [-26, 26].forEach((x) => P.push(mk("Pin boss", new THREE.CylinderGeometry(17, 17, 14, 48), 0xcfd3d9, Object.assign({ rot: [0, 0, Math.PI / 2], pos: [x, 24, 0] }, STEEL))));
    P.push(mk("Wrist pin", new THREE.CylinderGeometry(11, 11, 62, 48), 0x8f98a3, Object.assign({ rot: [0, 0, Math.PI / 2], pos: [0, 24, 0] }, STEEL)));
    return {
      parts: P, clip: halfCut(cut), focus: null, gridStep: 5,
      dims: (box) => cat(
        dim([-40, 0, box.max.z + 6], [40, 0, box.max.z + 6], [0, 0, 1], 1.8),
        dim([box.max.x + 6, 0, 0], [box.max.x + 6, 60, 0], [1, 0, 0], 1.8)),
      groundOffset: 0.0
    };
  }

  /* Connecting rod: 150 mm centres, 50 mm big end bore, 22 mm small end bore, 24 mm wide ends, I-beam shank. Lies flat. */
  function buildConrod(cut) {
    TEXBY.clear(); GLOSSY.clear();
    const L = 150, RB = 34, rb = 25, RS = 18, rs = 11, B = 24, WEB = 8, MET = 0xb9bfc8;
    const big = discShape(RB, 96); big.holes.push(ring(0, 0, rb, 64));
    const small = polyShape(ngon(RS, 72).map((p) => [p[0] + L, p[1]])); small.holes.push(ring(L, 0, rs, 48));
    const railTop = polyShape([[26, 15], [137, 10], [137, 6], [26, 11]]);
    const railBot = polyShape([[26, -15], [26, -11], [137, -6], [137, -10]]);
    const web = polyShape([[26, 11], [137, 6], [137, -6], [26, -11]]);
    const flat = [-Math.PI / 2, 0, 0], o = (y) => Object.assign({ rot: flat, pos: [0, y, 0] }, STEEL);
    const P = [
      mk("Big end", extrude(big, B), MET, o(0)),
      mk("Small end", extrude(small, B), MET, o(0)),
      mk("Upper flange", extrude(railTop, B), MET, o(0)),
      mk("Lower flange", extrude(railBot, B), MET, o(0)),
      mk("Web", extrude(web, WEB), MET, o((B - WEB) / 2))
    ];
    return {
      parts: P, clip: halfCut(cut), focus: null, gridStep: 5,
      dims: (box) => cat(
        dim([0, 0, box.max.z + 8], [L, 0, box.max.z + 8], [0, 0, 1], 2),
        dim([box.max.x + 6, 0, 0], [box.max.x + 6, B, 0], [1, 0, 0], 2)),
      groundOffset: 0.0
    };
  }

  /* ---------- crankshaft pieces for any number of throws ---------- */
  // PX: pin x positions; SG: +1 pin up, -1 pin down. 80 mm stroke, 50 mm mains, 44 mm pins, a counterweight on every web.
  function crankParts(PX, SG) {
    const MET = 0xb9bfc8, R = 40, WT = 16, P = [], n = PX.length;
    const webShape = (sg) => {
      const pts = ngon(27, 40).concat(ngon(21, 32).map((p) => [p[0], p[1] + sg * R]));
      for (let a = -62; a <= 62; a += 8) { const t = (-sg * 90 + a) * (Math.PI / 180); pts.push([50 * Math.cos(t), 50 * Math.sin(t)]); }
      return polyShape(hull(pts));
    };
    const ax = (name, r, len, x, y) => mk(name, new THREE.CylinderGeometry(r, r, len, 56), MET, Object.assign({ rot: [0, 0, Math.PI / 2], pos: [x, y || 0, 0] }, STEEL));
    PX.forEach((px, i) => {
      P.push(ax("Rod journal " + (i + 1), 22, 28, px, SG[i] * R));
      [px - 30, px + 14].forEach((x0) => P.push(mk("Web", extrude(webShape(SG[i]), WT), MET, Object.assign({ rot: [0, Math.PI / 2, 0], pos: [x0, 0, 0] }, STEEL))));
    });
    for (let i = 0; i < n - 1; i++) { const gap = PX[i + 1] - PX[i]; if (gap > 70) P.push(ax("Main journal " + (i + 2), 25, gap - 60, (PX[i] + PX[i + 1]) / 2, 0)); }
    const xl = PX[0] - 47.5, xr = PX[n - 1] + 47.5, xf = xr + 17.5;
    P.push(ax("Main journal 1", 25, 35, xl), ax("Main journal " + (n + 1), 25, 35, xr), ax("Nose", 15, 30, xl - 32.5));
    const fl = discShape(45, 72);
    for (let i = 0; i < 6; i++) { const a = (i / 6) * TAU; fl.holes.push(ring(35 * Math.cos(a), 35 * Math.sin(a), 5, 24)); }
    P.push(mk("Flywheel flange", extrude(fl, 14), MET, Object.assign({ rot: [0, Math.PI / 2, 0], pos: [xf, 0, 0], extra: new Float32Array([xl - 55, 0, 0, xf + 21, 0, 0]) }, STEEL)));
    return { parts: P, x0: xl - 47.5, x1: xf + 14 };
  }
  function buildCrank(cut) {
    TEXBY.clear(); GLOSSY.clear();
    const c = crankParts([-150, -50, 50, 150], [1, -1, -1, 1]);
    return {
      parts: c.parts, clip: halfCut(cut), focus: null, gridStep: 10,
      dims: (box) => cat(
        dim([c.x0, 0, box.max.z + 10], [c.x1, 0, box.max.z + 10], [0, 0, 1], 3),
        dim([box.max.x + 10, -45, 0], [box.max.x + 10, 45, 0], [1, 0, 0], 3)),
      groundOffset: 0.0
    };
  }

  /* ---------- engine pieces ---------- */
  const PISTON_PROF = [[0, 60], [38.6, 60], [39.6, 59], [39.6, 52], [36.5, 52], [36.5, 50], [39.6, 50], [39.6, 46], [36.5, 46], [36.5, 44], [39.6, 44], [39.6, 40],
    [36.5, 40], [36.5, 36], [39.9, 36], [39.9, 1], [38.9, 0], [33, 0], [33, 52], [0, 52]];
  // Piston with its pin bosses and wrist pin, pin centre at height yPin on axis x.
  function pistonParts(P, x, yPin) {
    P.push(revolve("Piston", PISTON_PROF, 0xcfd3d9, Object.assign({ segs: 72, pos: [x, yPin - 24, 0] }, STEEL)));
    [-26, 26].forEach((dx) => P.push(mk("Pin boss", new THREE.CylinderGeometry(17, 17, 14, 40), 0xcfd3d9, Object.assign({ rot: [0, 0, Math.PI / 2], pos: [x + dx, yPin, 0] }, STEEL))));
    P.push(mk("Wrist pin", new THREE.CylinderGeometry(10.9, 10.9, 62, 40), 0x8f98a3, Object.assign({ rot: [0, 0, Math.PI / 2], pos: [x, yPin, 0] }, STEEL)));
  }
  // Connecting rod standing up from a big end at height yBig, 150 mm centres, 24 mm wide, I-beam shank.
  function rodVertical(P, x, yBig) {
    const L = 150, RB = 34, rb = 25, RS = 18, rs = 11.2, B = 24, WEB = 8, MET = 0xb9bfc8;
    const big = discShape(RB, 72); big.holes.push(ring(0, 0, rb, 48));
    const small = polyShape(ngon(RS, 56).map((p) => [p[0], p[1] + L])); small.holes.push(ring(0, L, rs, 36));
    const tp = (pts) => pts.map((p) => [p[1], p[0]]);
    const railA = polyShape(tp([[26, 15], [137, 10], [137, 6], [26, 11]]));
    const railB = polyShape(tp([[26, -15], [26, -11], [137, -6], [137, -10]]));
    const web = polyShape(tp([[26, 11], [137, 6], [137, -6], [26, -11]]));
    const o = (dx) => Object.assign({ rot: [0, Math.PI / 2, 0], pos: [x - B / 2 + dx, yBig, 0] }, STEEL);
    P.push(mk("Rod big end", extrude(big, B), MET, o(0)), mk("Rod small end", extrude(small, B), MET, o(0)),
      mk("Rod flange", extrude(railA, B), MET, o(0)), mk("Rod flange", extrude(railB, B), MET, o(0)), mk("Rod web", extrude(web, WEB), MET, o((B - WEB) / 2)));
  }
  // A complete engine: block, crankshaft, pistons, rods, head, cover, plugs, ports, manifolds, sump, flywheel, pulley.
  // 80 mm bore, 80 mm stroke, 150 mm rods. Cylinders at the ends of their strokes (TDC or BDC), so every rod stands vertical.
  function engineParts(PX, SG) {
    const MET = 0xaab3c0, R = 40, LR = 150, DECK = 227, P = [], n = PX.length, BL = { r: 0.55, m: 0.35 }, flat = [-Math.PI / 2, 0, 0];
    const c = crankParts(PX, SG);
    c.parts.forEach((p) => P.push(p));
    PX.forEach((px, i) => { pistonParts(P, px, SG[i] * R + LR); rodVertical(P, px, SG[i] * R); });
    const cx = (PX[0] + PX[n - 1]) / 2, Lb = (PX[n - 1] - PX[0]) + 130;
    const outline = () => polyShape(rrPts(Lb, 110, 6, 6).map((p) => [p[0] + cx, p[1]]));
    const deck = outline(); PX.forEach((px) => deck.holes.push(ring(px, 0, 40.3, 64)));
    P.push(mk("Cylinder block", extrude(deck, DECK - 75), MET, Object.assign({ rot: flat, pos: [0, 75, 0] }, BL)));
    [-1, 1].forEach((s) => P.push(mk("Crankcase wall", new THREE.BoxGeometry(Lb, 165, 10), MET, Object.assign({ pos: [cx, -7.5, s * 50] }, BL))));
    [-1, 1].forEach((s) => P.push(mk("Crankcase end", new THREE.BoxGeometry(10, 165, 110), MET, Object.assign({ pos: [cx + s * (Lb / 2 - 5), -7.5, 0] }, BL))));
    P.push(mk("Oil sump", new THREE.BoxGeometry(Lb - 20, 40, 100), 0x8d96a3, Object.assign({ pos: [cx, -110, 0] }, BL)));
    P.push(mk("Cylinder head", extrude(outline(), 40), 0xb9c1cc, Object.assign({ rot: flat, pos: [0, DECK, 0] }, BL)));
    P.push(mk("Cam cover", new THREE.BoxGeometry(Math.max(60, Lb - 50), 36, 70), 0x8d96a3, Object.assign({ pos: [cx, DECK + 58, 0] }, BL)));
    PX.forEach((px) => {
      P.push(mk("Spark plug", new THREE.CylinderGeometry(7, 7, 38, 24), 0xe3e6ea, { r: 0.35, m: 0.5, pos: [px, DECK + 95, 0] }));
      [-1, 1].forEach((s) => P.push(mk("Port", new THREE.CylinderGeometry(15, 15, 30, 32), MET, Object.assign({ rot: [Math.PI / 2, 0, 0], pos: [px, DECK + 22, s * 70] }, BL))));
    });
    const ml = Math.max(40, (PX[n - 1] - PX[0]) + 60);
    [-1, 1].forEach((s) => P.push(mk("Manifold", new THREE.CylinderGeometry(21, 21, ml, 40), 0x8d96a3, Object.assign({ rot: [0, 0, Math.PI / 2], pos: [cx, DECK + 22, s * 90] }, BL))));
    P.push(revolve("Flywheel", [[0, 0], [126, 0], [130, 4], [130, 24], [126, 28], [0, 28]], 0x9aa0a8, Object.assign({ segs: 96, rot: [0, 0, Math.PI / 2], pos: [c.x1 + 28, 0, 0] }, STEEL)));
    P.push(revolve("Crank pulley", [[0, 0], [24, 0], [24, 8], [50, 8], [50, 12], [46, 17], [50, 22], [50, 26], [24, 26], [24, 36], [0, 36]], 0x9aa0a8, Object.assign({ segs: 72, rot: [0, 0, Math.PI / 2], pos: [c.x0 + 24, 0, 0] }, STEEL)));
    return P;
  }
  const engineDims = (box) => cat(
    dim([box.min.x, box.min.y, box.max.z + 12], [box.max.x, box.min.y, box.max.z + 12], [0, 0, 1], 4),
    dim([box.max.x + 12, box.min.y, 0], [box.max.x + 12, box.max.y, 0], [1, 0, 0], 4));
  function buildEngine4(cut) {
    TEXBY.clear(); GLOSSY.clear();
    return { parts: engineParts([-150, -50, 50, 150], [1, -1, -1, 1]), clip: halfCut(cut), focus: null, gridStep: 10, dims: engineDims, groundOffset: 0.0 };
  }
  function buildEngine1(cut) {
    TEXBY.clear(); GLOSSY.clear();
    return { parts: engineParts([0], [1]), clip: halfCut(cut), focus: null, gridStep: 10, dims: engineDims, groundOffset: 0.0 };
  }

  /* Camshaft for a four-cylinder engine: five journals, eight lobes (base circle 32 mm, 8 mm lift), timing sprocket. Lies along X. */
  function buildCamshaft(cut) {
    TEXBY.clear(); GLOSSY.clear();
    const MET = 0xb9bfc8, P = [];
    const ax = (name, r, len, x) => mk(name, new THREE.CylinderGeometry(r, r, len, 48), MET, Object.assign({ rot: [0, 0, Math.PI / 2], pos: [x, 0, 0] }, STEEL));
    [-200, -100, 0, 100, 200].forEach((x, i) => P.push(ax("Journal " + (i + 1), 14, 30, x)));
    P.push(mk("Core", new THREE.CylinderGeometry(10, 10, 460, 40), MET, Object.assign({ rot: [0, 0, Math.PI / 2], pos: [-10, 0, 0], extra: new Float32Array([-262, 0, 0, 232, 0, 0]) }, STEEL)));
    const lobe = (psi) => {
      const pts = [];
      for (let k = 0; k < 90; k++) {
        const a = (k / 90) * TAU, d = Math.atan2(Math.sin(a - psi), Math.cos(a - psi)), r = 16 + 8 * Math.pow(Math.max(0, Math.cos(d * 1.8)), 2);
        pts.push([r * Math.cos(a), r * Math.sin(a)]);
      }
      return polyShape(pts);
    };
    [-150, -50, 50, 150].forEach((px, i) => [-25, 25].forEach((dx, j) =>
      P.push(mk("Cam lobe", extrude(lobe(((i * 90 + j * 45 + 20) * Math.PI) / 180), 12), MET, Object.assign({ rot: [0, Math.PI / 2, 0], pos: [px + dx - 6, 0, 0] }, STEEL)))));
    const teeth = [];
    for (let k = 0; k < 72; k++) { const a = (k / 72) * TAU, r = k % 2 === 0 ? 38 : 34; teeth.push([r * Math.cos(a), r * Math.sin(a)]); }
    const spr = polyShape(teeth); spr.holes.push(ring(0, 0, 10, 32));
    P.push(mk("Timing sprocket", extrude(spr, 12), MET, Object.assign({ rot: [0, Math.PI / 2, 0], pos: [-246, 0, 0] }, STEEL)));
    return {
      parts: P, clip: halfCut(cut), focus: null, gridStep: 10,
      dims: (box) => cat(
        dim([-246, 0, box.max.z + 8], [220, 0, box.max.z + 8], [0, 0, 1], 3),
        dim([box.max.x + 8, -38, 0], [box.max.x + 8, 38, 0], [1, 0, 0], 3)),
      groundOffset: 0.0
    };
  }

  /* Ventilated brake disc: 300 mm OD, 28 mm over the friction rings, 5 x 114.3 bolt pattern, 67.1 mm centre bore, 36 straight vanes. */
  function buildBrakeDisc(cut) {
    TEXBY.clear(); GLOSSY.clear();
    const MET = 0x9aa0a8, flat = [-Math.PI / 2, 0, 0], P = [];
    const ringShape = (ro, ri) => { const s = discShape(ro, 120); s.holes.push(ring(0, 0, ri, 96)); return s; };
    P.push(mk("Rear friction ring", extrude(ringShape(150, 90), 9), MET, Object.assign({ rot: flat, pos: [0, 0, 0] }, STEEL)));
    P.push(mk("Front friction ring", extrude(ringShape(150, 74), 9), MET, Object.assign({ rot: flat, pos: [0, 19, 0] }, STEEL)));
    const top = discShape(80, 96);
    top.holes.push(ring(0, 0, 33.55, 64));
    for (let i = 0; i < 5; i++) { const a = Math.PI / 2 + (i / 5) * TAU; top.holes.push(ring(57.15 * Math.cos(a), 57.15 * Math.sin(a), 7, 32)); }
    P.push(mk("Mounting face", extrude(top, 6), MET, Object.assign({ rot: flat, pos: [0, 40, 0], extra: circY(57.15, 46.04, 72) }, STEEL)));
    P.push(revolve("Hat wall", [[74, 19], [80, 19], [80, 46], [74, 46]], MET, Object.assign({ segs: 96 }, STEEL)));
    for (let k = 0; k < 36; k++) {
      const t = (k / 36) * TAU;
      P.push(mk("Vane", new THREE.BoxGeometry(60, 10, 5), MET, Object.assign({ rot: [0, t, 0], pos: [120 * Math.cos(t), 14, -120 * Math.sin(t)] }, STEEL)));
    }
    return {
      parts: P, clip: halfCut(cut), focus: null, gridStep: 10,
      dims: (box) => cat(
        dim([box.min.x, 0, box.max.z + 10], [box.max.x, 0, box.max.z + 10], [0, 0, 1], 3),
        dim([box.max.x + 10, 0, 0], [box.max.x + 10, 46, 0], [1, 0, 0], 3)),
      groundOffset: 0.0
    };
  }

  /* Compression spring: 3 mm wire, 24 mm OD, 8 active coils, closed and ground ends, 60 mm free length. */
  function buildSpring(cut) {
    TEXBY.clear(); GLOSSY.clear();
    const d = 3, Rm = 10.5, Na = 8, pitch = (60 - 2 * d - d) / Na + 0;   // overall 60 = 2 closed turns + Na * pitch + wire dia
    const yAt = (u) => (u <= 1 ? d * u : u <= 1 + Na ? d + pitch * (u - 1) : d + pitch * Na + d * (u - 1 - Na)) + d / 2;
    const turns = Na + 2, per = 36, N = turns * per, pts = [], outer = [], inner = [];
    for (let i = 0; i <= N; i++) {
      const u = i / per, a = u * TAU, y = yAt(u);
      pts.push(new THREE.Vector3(Rm * Math.cos(a), y, Rm * Math.sin(a)));
      outer.push([(Rm + d / 2) * Math.cos(a), y, (Rm + d / 2) * Math.sin(a)]);
      inner.push([(Rm - d / 2) * Math.cos(a), y, (Rm - d / 2) * Math.sin(a)]);
    }
    const ln = [];
    for (let i = 0; i < N; i++) ln.push(outer[i][0], outer[i][1], outer[i][2], outer[i + 1][0], outer[i + 1][1], outer[i + 1][2], inner[i][0], inner[i][1], inner[i][2], inner[i + 1][0], inner[i + 1][1], inner[i + 1][2]);
    const curve = new THREE.CatmullRomCurve3(pts, false, "centripetal");
    const lines = new Float32Array(ln);
    const P = [mk("Compression spring", new THREE.TubeGeometry(curve, N * 2, d / 2, 14, false), 0x9fb0c6, { r: 0.35, m: 0.5, seg: () => lines })];
    return {
      parts: P, clip: halfCut(cut), focus: null, gridStep: 5,
      dims: (box) => cat(
        dim([-12, 0, box.max.z + 6], [12, 0, box.max.z + 6], [0, 0, 1], 1.5),
        dim([box.max.x + 6, 0, 0], [box.max.x + 6, 60, 0], [1, 0, 0], 1.5)),
      groundOffset: 0.0
    };
  }

  /* M12 x 35 hex bolt (18 mm across flats, 7.5 mm head) through a 12 mm plate, with a 13/24/2.5 washer and an M12 nut (10.8 mm). */
  function buildBolt(cut) {
    TEXBY.clear(); GLOSSY.clear();
    const AF = 18, RH = AF / (2 * Math.cos(Math.PI / 6)), flat = [-Math.PI / 2, 0, 0], MET = 0x8d949e;
    const yN = 0, tN = 10.8, yW = tN, tW = 2.5, yP = yW + tW, tP = 12, yH = yP + tP, tH = 7.5, LEN = 35;
    const hex = () => polyShape(ngon(RH, 6, Math.PI / 6));
    const nut = hex(); nut.holes.push(ring(0, 0, 6, 40));
    const wash = discShape(12, 64); wash.holes.push(ring(0, 0, 6.6, 40));
    const plate = polyShape(rrPts(40, 40, 3, 6)); plate.holes.push(ring(0, 0, 6.75, 40));
    const yTip = yH - LEN, threads = [];
    for (let y = yTip + 1; y < yN; y += 1.75) threads.push(circY(6.05, y, 40));
    const P = [
      mk("Hex nut", extrude(nut, tN), MET, Object.assign({ rot: flat, pos: [0, yN, 0] }, STEEL)),
      mk("Washer", extrude(wash, tW), 0xaab2bc, Object.assign({ rot: flat, pos: [0, yW, 0] }, STEEL)),
      mk("Clamped plate", extrude(plate, tP), 0xb9bfc8, Object.assign({ rot: flat, pos: [0, yP, 0] }, STEEL)),
      mk("Hex bolt head", extrude(hex(), tH), MET, Object.assign({ rot: flat, pos: [0, yH, 0] }, STEEL)),
      mk("Bolt shank", new THREE.CylinderGeometry(5.95, 5.95, LEN, 40), MET, Object.assign({ pos: [0, yH - LEN / 2, 0], extra: cat.apply(null, threads.concat([new Float32Array([0, yTip - 3, 0, 0, yH + tH + 3, 0])])) }, STEEL))
    ];
    return {
      parts: P, clip: halfCut(cut), focus: null, gridStep: 5,
      dims: (box) => cat(
        dim([box.max.x + 7, yTip, 0], [box.max.x + 7, yH, 0], [1, 0, 0], 1.6),
        dim([-20, yTip, box.max.z + 6], [20, yTip, box.max.z + 6], [0, 0, 1], 1.6)),
      groundOffset: 0.0
    };
  }

  /* Enclosure, exploded: 100 x 60 x 30 mm housing with 2.5 mm walls and four screw bosses, vented 2.5 mm lid above it. */
  function buildEnclosure(cut) {
    TEXBY.clear(); GLOSSY.clear();
    const W = 100, D = 60, H = 30, t = 2.5, CR = 4, flat = [-Math.PI / 2, 0, 0], PL = 0xa9b3c0, o = { r: 0.55, m: 0.1 };
    const outline = () => polyShape(rrPts(W, D, CR, 6));
    const walls = outline(); walls.holes.push(pathOf(rrPts(W - 2 * t, D - 2 * t, CR - t, 6)));
    const lid = outline();
    const bx = W / 2 - t - 4, bz = D / 2 - t - 4;
    [[1, 1], [-1, 1], [-1, -1], [1, -1]].forEach((c) => lid.holes.push(ring(c[0] * bx, c[1] * bz, 1.6, 24)));
    [-8, 0, 8].forEach((y) => lid.holes.push(pathOf(rrPts(26, 2.4, 1.1, 3).map((p) => [p[0], p[1] + y]))));
    const P = [
      mk("Housing floor", extrude(outline(), t), PL, Object.assign({ rot: flat, pos: [0, 0, 0] }, o)),
      mk("Housing walls", extrude(walls, H - t), PL, Object.assign({ rot: flat, pos: [0, t, 0] }, o)),
      mk("Lid", extrude(lid, t), 0xbac3cf, Object.assign({ rot: flat, pos: [0, H + 14, 0] }, o))
    ];
    [[1, 1], [-1, 1], [-1, -1], [1, -1]].forEach((c) => P.push(revolve("Screw boss", [[1.3, t], [4, t], [4, 24], [1.3, 24]], PL, Object.assign({ segs: 40, pos: [c[0] * bx, 0, -c[1] * bz] }, o))));
    return {
      parts: P, clip: halfCut(cut), focus: null, gridStep: 5, partCount: 2,
      dims: (box) => cat(
        dim([-W / 2, 0, box.max.z + 6], [W / 2, 0, box.max.z + 6], [0, 0, 1], 1.8),
        dim([box.max.x + 6, 0, 0], [box.max.x + 6, H, 0], [1, 0, 0], 1.8)),
      groundOffset: 0.0
    };
  }

  const SCENES = [
    {
      id: "engine-4", label: "Inline-4 engine", build: buildEngine4, unit: "mm", partCount: 26,
      prompt: "Inline-4 engine, 80 mm bore, 80 mm stroke, 100 mm bore pitch, 150 mm connecting rods",
      alt: "An inline four-cylinder engine with pistons, connecting rods and crankshaft drawn as a blueprint line by line, then turning into a solid 3D model",
      envelope: "length × depth × height",
      cam: { from: { az: 0.0, el: 0.7 }, to: { az: 0.5, el: 0.3 } },
      camCut: { from: { az: 0.0, el: 0.7 }, to: { az: 0.12, el: 0.22 } }
    },
    {
      id: "engine-1", label: "Single-cyl engine", build: buildEngine1, unit: "mm", partCount: 14,
      prompt: "Single-cylinder engine, 80 mm bore, 80 mm stroke, 150 mm connecting rod",
      alt: "A single-cylinder engine with piston, connecting rod and crankshaft drawn as a blueprint line by line, then turning into a solid 3D model",
      envelope: "length × depth × height",
      cam: { from: { az: 0.0, el: 0.7 }, to: { az: 0.55, el: 0.3 } },
      camCut: { from: { az: 0.0, el: 0.7 }, to: { az: 0.14, el: 0.22 } }
    },
    {
      id: "camshaft", label: "Camshaft", build: buildCamshaft, unit: "mm", partCount: 1,
      prompt: "Camshaft for a 4-cylinder engine, 8 lobes, 5 journals, 100 mm cylinder pitch",
      alt: "A camshaft with eight lobes drawn as a blueprint line by line, then turning into a solid 3D model",
      envelope: "length × depth × height",
      cam: { from: { az: 0.0, el: 1.0 }, to: { az: 0.4, el: 0.35 } },
      camCut: { from: { az: 0.0, el: 1.0 }, to: { az: 0.14, el: 0.3 } }
    },
    {
      id: "gear", label: "Gear", build: buildGear, unit: "mm", partCount: 1,
      prompt: "Spur gear, module 2, 20 teeth, 10 mm thick, 5 mm bore",
      alt: "A 20-tooth spur gear drawn as a blueprint line by line, then turning into a solid 3D model",
      envelope: "width × depth × height",
      cam: { from: { az: 0.0, el: 1.34 }, to: { az: 0.62, el: 0.5 } },
      camCut: { from: { az: 0.0, el: 1.34 }, to: { az: 0.12, el: 0.62 } }
    },
    {
      id: "bracket", label: "Bracket", build: buildBracket, unit: "mm", partCount: 1,
      prompt: "L-bracket, 50 mm wide, 60 mm tall, 40 mm deep, 3 mm thick, 2 holes per leg",
      alt: "An L-bracket with two holes in each leg drawn as a blueprint line by line, then turning into a solid 3D model",
      envelope: "width × depth × height",
      cam: { from: { az: 0.2, el: 1.2 }, to: { az: 0.72, el: 0.42 } },
      camCut: { from: { az: 0.2, el: 1.2 }, to: { az: 1.15, el: 0.4 } }
    },
    {
      id: "flange", label: "Flange", build: buildFlange, unit: "mm", partCount: 1,
      prompt: "Round flange, 100 mm diameter, 12 mm thick, 6 holes on a 75 mm circle, 30 mm bore, 50 mm hub",
      alt: "A round flange with six bolt holes and a raised hub drawn as a blueprint line by line, then turning into a solid 3D model",
      envelope: "width × depth × height",
      cam: { from: { az: 0.0, el: 1.34 }, to: { az: 0.6, el: 0.55 } },
      camCut: { from: { az: 0.0, el: 1.34 }, to: { az: 0.12, el: 0.5 } }
    },
    {
      id: "shaft", label: "Shaft", build: buildShaft, unit: "mm", partCount: 1,
      prompt: "Stepped shaft, 160 mm long, 20, 25 and 30 mm diameters, 1 mm chamfers",
      alt: "A stepped shaft drawn as a blueprint line by line, then turning into a solid 3D model",
      envelope: "length × depth × height",
      cam: { from: { az: 0.0, el: 1.3 }, to: { az: 0.5, el: 0.4 } },
      camCut: { from: { az: 0.0, el: 1.3 }, to: { az: 0.15, el: 0.45 } }
    },
    {
      id: "bearing", label: "Bearing", build: buildBearing, unit: "mm", partCount: 11,
      prompt: "Deep groove ball bearing 6205, 25 mm bore, 52 mm outside diameter, 15 mm wide, 9 balls",
      alt: "A deep groove ball bearing with an inner ring, outer ring and nine balls drawn as a blueprint line by line, then turning into a solid 3D model",
      envelope: "width × depth × height",
      cam: { from: { az: 0.0, el: 1.34 }, to: { az: 0.6, el: 0.55 } },
      camCut: { from: { az: 0.0, el: 1.34 }, to: { az: 0.2, el: 0.62 } }
    },
    {
      id: "pulley", label: "Pulley", build: buildPulley, unit: "mm", partCount: 1,
      prompt: "V-belt pulley, 120 mm diameter, single groove, 20 mm bore, 40 mm hub",
      alt: "A V-belt pulley with a single groove drawn as a blueprint line by line, then turning into a solid 3D model",
      envelope: "width × depth × height",
      cam: { from: { az: 0.0, el: 1.34 }, to: { az: 0.6, el: 0.5 } },
      camCut: { from: { az: 0.0, el: 1.34 }, to: { az: 0.15, el: 0.45 } }
    },
    {
      id: "piston", label: "Piston", build: buildPiston, unit: "mm", partCount: 2,
      prompt: "Piston, 80 mm bore, 60 mm tall, three ring grooves, 22 mm wrist pin",
      alt: "A piston with ring grooves and a wrist pin drawn as a blueprint line by line, then turning into a solid 3D model",
      envelope: "width × depth × height",
      cam: { from: { az: 0.0, el: 1.0 }, to: { az: 0.7, el: 0.4 } },
      camCut: { from: { az: 0.0, el: 1.0 }, to: { az: 0.18, el: 0.4 } }
    },
    {
      id: "conrod", label: "Con-rod", build: buildConrod, unit: "mm", partCount: 1,
      prompt: "Connecting rod, 150 mm centres, 50 mm big end bore, 22 mm small end bore, I-beam shank",
      alt: "A connecting rod with an I-beam shank drawn as a blueprint line by line, then turning into a solid 3D model",
      envelope: "length × depth × height",
      cam: { from: { az: 0.0, el: 1.34 }, to: { az: 0.45, el: 0.62 } },
      camCut: { from: { az: 0.0, el: 1.34 }, to: { az: 0.2, el: 0.55 } }
    },
    {
      id: "crankshaft", label: "Crankshaft", build: buildCrank, unit: "mm", partCount: 1,
      prompt: "Inline-4 crankshaft, 80 mm stroke, five main journals, eight counterweights, flywheel flange",
      alt: "An inline-four crankshaft drawn as a blueprint line by line, then turning into a solid 3D model",
      envelope: "length × depth × height",
      cam: { from: { az: 0.0, el: 0.9 }, to: { az: 0.38, el: 0.32 } },
      camCut: { from: { az: 0.0, el: 0.9 }, to: { az: 0.14, el: 0.3 } }
    },
    {
      id: "brake-disc", label: "Brake disc", build: buildBrakeDisc, unit: "mm", partCount: 1,
      prompt: "Ventilated brake disc, 300 mm diameter, 28 mm thick, 5 x 114.3 bolt pattern, 67 mm centre bore",
      alt: "A ventilated brake disc drawn as a blueprint line by line, then turning into a solid 3D model",
      envelope: "width × depth × height",
      cam: { from: { az: 0.0, el: 1.34 }, to: { az: 0.6, el: 0.5 } },
      camCut: { from: { az: 0.0, el: 1.34 }, to: { az: 0.22, el: 0.5 } }
    },
    {
      id: "spring", label: "Spring", build: buildSpring, unit: "mm", partCount: 1,
      prompt: "Compression spring, 3 mm wire, 24 mm outside diameter, 60 mm free length, 8 active coils",
      alt: "A compression spring drawn as a blueprint line by line, then turning into a solid 3D model",
      envelope: "width × depth × height",
      cam: { from: { az: 0.0, el: 0.7 }, to: { az: 0.7, el: 0.3 } },
      camCut: { from: { az: 0.0, el: 0.7 }, to: { az: 0.25, el: 0.3 } }
    },
    {
      id: "bolt", label: "Bolt", build: buildBolt, unit: "mm", partCount: 4,
      prompt: "M12 x 35 hex bolt through a 12 mm plate, with washer and nut",
      alt: "A hex bolt through a plate with a washer and nut drawn as a blueprint line by line, then turning into a solid 3D model",
      envelope: "width × depth × height",
      cam: { from: { az: 0.2, el: 0.8 }, to: { az: 0.8, el: 0.38 } },
      camCut: { from: { az: 0.2, el: 0.8 }, to: { az: 0.25, el: 0.35 } }
    },
    {
      id: "enclosure", label: "Enclosure", build: buildEnclosure, unit: "mm", partCount: 2,
      prompt: "Enclosure, 100 x 60 x 30 mm, 2.5 mm walls, four screw bosses, vented lid",
      alt: "An enclosure with four screw bosses and a vented lid drawn as a blueprint line by line, then turning into a solid 3D model",
      envelope: "width × depth × height",
      cam: { from: { az: 0.2, el: 1.0 }, to: { az: 0.75, el: 0.5 } },
      camCut: { from: { az: 0.2, el: 1.0 }, to: { az: 0.25, el: 0.5 } }
    }
  ];
  { const ORDER = ["gear","engine-4","engine-1","crankshaft","camshaft","piston","conrod","bearing","pulley","brake-disc","flange","shaft","bracket","spring","bolt","enclosure"]; SCENES.sort((a, b) => ORDER.indexOf(a.id) - ORDER.indexOf(b.id)); }

  /* ---------- runtime state ---------- */
  const cutBtn = document.getElementById("bpCut");
  // The tab row is built from the scene list, so the tabs can never disagree with the scenes that are loaded.
  const tabRow = stage.parentNode.querySelector(".bp-tabs");
  if (tabRow) {
    tabRow.innerHTML = "";
    SCENES.forEach((sc, k) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "bp-tab mono"; b.setAttribute("data-scene", sc.id);
      b.setAttribute("aria-pressed", k === 0 ? "true" : "false"); b.textContent = sc.label;
      tabRow.appendChild(b);
    });
  }
  const el = {
    tabs: Array.prototype.slice.call(stage.parentNode.querySelectorAll(".bp-tab")),
    prompt: document.getElementById("bpPrompt"),
    parts: document.getElementById("bpParts"),
    size: document.getElementById("bpSize"),
    sizeLabel: document.getElementById("bpSizeLabel")
  };
  const S = {
    index: -1, cut: false, t: 0, idle: 0, out: 0, next: -1, userPicked: false, visible: true,
    center: new THREE.Vector3(), dist: 30, cur: null,
    structN: 0, dimN: 0, lines: null, dimLines: null, grid: null, group: null, promptText: ""
  };

  const envBox = () => { const b = new THREE.Box3(); S.group.children.forEach((m) => { if (!m.userData.dress) b.expandByObject(m); }); return b; };
  const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
  const lerp = (a, b, k) => a + (b - a) * k;
  const easeIO = (k) => 0.5 - 0.5 * Math.cos(Math.PI * k);
  const easeCubic = (k) => (k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2);

  function disposeCurrent() {
    [S.group, S.lines, S.dimLines, S.grid].forEach((o) => {
      if (!o) return;
      scene.remove(o);
      o.traverse((n) => {
        if (n.geometry) n.geometry.dispose();
        if (n.material) (Array.isArray(n.material) ? n.material : [n.material]).forEach((m) => m.dispose());
      });
    });
    S.group = S.lines = S.dimLines = S.grid = null;
  }

  function lineObj(arr, color) {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(arr, 3));
    g.setDrawRange(0, 0);
    return new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color, transparent: true }));
  }

  function loadScene(i) {
    disposeCurrent();
    MATC.clear();
    const def = SCENES[i];
    const built = def.build(S.cut);
    const cl = built.clip ? [built.clip] : [];
    renderer.clippingPlanes = cl; if (dockR) dockR.clippingPlanes = cl;
    if (cutBtn) cutBtn.setAttribute("aria-pressed", S.cut ? "true" : "false");
    S.index = i; S.cur = def; S.t = 0; S.idle = 0; S.out = 0; S.next = -1;

    S.group = new THREE.Group();
    built.parts.forEach((p) => S.group.add(p.mesh));
    scene.add(S.group);
    S.anims = built.parts.filter((p) => p.mesh.userData.anim).map((p) => p.mesh);
    S.group.updateMatrixWorld(true);
    const box = envBox();
    const dimArr = built.dims(box);
    const full = box.clone();
    for (let k = 0; k < dimArr.length; k += 3) full.expandByPoint(new THREE.Vector3(dimArr[k], dimArr[k + 1], dimArr[k + 2]));

    const structArr = cat.apply(null, built.parts.map((p) => p.seg));
    S.lines = lineObj(structArr, COL.line);
    S.dimLines = lineObj(dimArr, COL.dim);
    scene.add(S.lines, S.dimLines);
    S.structN = structArr.length / 3 | 0;
    S.dimN = dimArr.length / 3 | 0;

    const size = box.getSize(new THREE.Vector3());
    const sphere = full.getBoundingSphere(new THREE.Sphere());
    const fs = built.focus ? built.focus.getBoundingSphere(new THREE.Sphere()) : sphere;
    S.center.copy(fs.center); S.focusR = built.focus ? fs.radius : 0;
    fitDistance(fs.radius);

    const gridSize = Math.ceil(sphere.radius * 2.6 / 2) * 2;
    const step = built.gridStep || 1;
    S.grid = new THREE.GridHelper(Math.ceil(gridSize / step) * step, Math.ceil(gridSize / step), COL.grid, COL.grid);
    S.grid.position.set(sphere.center.x, box.min.y - built.groundOffset, sphere.center.z);
    S.grid.material.transparent = true;
    S.grid.material.opacity = 0;
    scene.add(S.grid);

    S.promptText = def.prompt;
    const nParts = def.partCount != null ? def.partCount : built.parts.filter((p) => !p.dress).length;
    if (el.parts) el.parts.textContent = nParts + (nParts === 1 ? " part" : " parts");
    if (el.size) el.size.textContent = size.x.toFixed(1) + " × " + size.z.toFixed(1) + " × " + size.y.toFixed(1) + " " + (def.unit || "m");
    if (el.sizeLabel) el.sizeLabel.textContent = "Envelope, " + def.envelope;
    host.setAttribute("role", "img");
    host.setAttribute("aria-label", def.alt);
    el.tabs.forEach((b, k) => b.setAttribute("aria-pressed", k === i ? "true" : "false"));
    const act = el.tabs[i];
    if (act && act.parentNode && act.parentNode.scrollTo) act.parentNode.scrollTo({ left: act.offsetLeft - (act.parentNode.clientWidth - act.offsetWidth) / 2, behavior: reduce ? "auto" : "smooth" });
    window.dispatchEvent(new CustomEvent("bp-scene", { detail: { index: i, id: def.id, label: def.label } }));
    update(reduce ? 99 : 0, 1);
  }

  function fitDistance(radius) {
    const aspect = camera.aspect || 1.25;
    const base = (radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2))) * 0.88;
    S.dist = aspect < 1.25 ? base * Math.pow(1.25 / aspect, 0.85) : base;
  }

  function placeCamera(az, el_, cam, scale) {
    cam = cam || camera;
    const c = S.center, d = S.dist * (scale || 1);
    // Scale the clipping planes with the part so big parts (a 300 mm brake disc, a 470 mm crankshaft) are not cut off.
    const far = Math.max(400, d * 6), near = Math.max(0.1, d * 0.02);
    if (cam.far !== far || cam.near !== near) { cam.far = far; cam.near = near; cam.updateProjectionMatrix(); }
    cam.position.set(
      c.x + d * Math.sin(az) * Math.cos(el_),
      c.y + d * Math.sin(el_),
      c.z + d * Math.cos(az) * Math.cos(el_));
    cam.lookAt(c);
  }

  /* k = overall visibility multiplier (used to erase the old scene before a swap) */
  function update(t, k) {
    if (!reduce && S.anims && S.anims.length) { const ta = performance.now() / 1000; for (const m of S.anims) m.userData.anim(ta, m); }
    const drawP = clamp((t - 0.3) / 3.3, 0, 1);
    const sf = clamp(drawP / 0.86, 0, 1), df = clamp((drawP - 0.86) / 0.14, 0, 1);
    const sN = Math.floor(S.structN * easeIO(sf) * k) & ~1;
    const dN = Math.floor(S.dimN * easeIO(df) * k) & ~1;
    S.lines.geometry.setDrawRange(0, sN);
    S.dimLines.geometry.setDrawRange(0, dN);

    const solid = easeIO(clamp((t - 3.6) / 1.1, 0, 1)) * k;
    S.group.traverse((n) => {
      if (!n.isMesh) return;
      const m = n.material, mx = m.userData.max, tr = !(mx >= 1 && solid > 0.999);
      m.opacity = mx * solid;
      if (m.transparent !== tr) { m.transparent = tr; m.needsUpdate = true; }
      n.visible = solid > 0.01;
    });
    S.lines.material.color.setHex(COL.line).lerp(new THREE.Color(COL.lineSolid), solid);
    S.lines.material.opacity = Math.max(0.3, 1 - solid * 0.7);
    S.grid.material.opacity = 0.2 * clamp(t / 0.8, 0, 1) * k;

    const u = easeCubic(clamp((t - 0.8) / 4.6, 0, 1));
    const c = S.cut && S.cur.camCut ? S.cur.camCut : S.cur.cam;
    placeCamera(lerp(c.from.az, c.to.az, u) + S.idle, lerp(c.from.el, c.to.el, u));

    if (el.prompt) {
      const n = Math.floor(clamp((t - 0.05) / 1.3, 0, 1) * S.promptText.length);
      el.prompt.textContent = S.promptText.slice(0, n);
      el.prompt.classList.toggle("done", n >= S.promptText.length);
    }
  }

  function toggleCut() {
    if (S.out > 0) return;
    S.userPicked = true; S.cut = !S.cut;
    if (reduce) { loadScene(S.index); renderOnce(); return; }
    S.next = S.index; S.out = 0.35;
  }
  function switchTo(i, byUser) {
    if (i === S.index || S.out > 0) return;
    if (byUser) S.userPicked = true;
    if (reduce) { loadScene(i); renderOnce(); return; }
    S.next = i; S.out = 0.35;
  }

  /* ---------- sizing, loop, wiring ---------- */
  function resize() {
    const w = host.clientWidth || 600, h = host.clientHeight || 480;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, w < 520 ? 1.5 : 2));
    renderer.setSize(w, h, false);
    renderer.domElement.style.width = "100%";
    renderer.domElement.style.height = "100%";
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    if (S.cur && S.group) {
      const box = envBox();
      fitDistance(S.focusR || box.getBoundingSphere(new THREE.Sphere()).radius * 1.18);
    }
  }
  const renderOnce = () => { if (S.cur) { update(reduce ? 99 : S.t, 1); renderer.render(scene, camera); } };

  /* Docked copy: once the hero scrolls away, a small model stays on screen and turns with the scroll. */
  const dock = document.getElementById("bpDock");
  const dockHost = dock && dock.querySelector(".bp-dock-canvas");
  let dockR = null, dockCam = null, dockOn = false;
  if (dockHost && !reduce) {
    try {
      dockR = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "low-power" });
      dockR.setClearColor(0x000000, 0);
      dockR.toneMapping = THREE.ACESFilmicToneMapping; dockR.toneMappingExposure = 1.15; dockR.localClippingEnabled = true;
      dockR.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      dockR.setSize(dockHost.clientWidth || 168, dockHost.clientHeight || 168, false);
      dockR.domElement.style.cssText = "width:100%;height:100%;display:block";
      dockHost.appendChild(dockR.domElement);
      dockCam = new THREE.PerspectiveCamera(30, 1, 0.1, 400);
    } catch (err) { dockR = null; }
  }
  function syncDock() {
    dockOn = !!dockR && !S.visible && window.innerWidth >= 900 && window.scrollY > 200;
    if (dock) dock.classList.toggle("on", dockOn);
  }
  window.addEventListener("resize", syncDock);
  window.addEventListener("scroll", syncDock, { passive: true });

  let last = performance.now();
  function frame(now) {
    requestAnimationFrame(frame);
    if (!(S.visible || dockOn) || document.hidden) { last = now; return; }
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    if (S.out > 0) {
      S.out -= dt;
      update(S.t, clamp(S.out / 0.35, 0, 1));
      if (S.out <= 0) loadScene(S.next);
    } else {
      S.t += dt;
      S.idle += dt * 0.16 * clamp((S.t - 5.4) / 1.5, 0, 1);
      update(S.t, 1);
      if (!S.userPicked && S.t > 12.5) switchTo((S.index + 1) % SCENES.length, false);
    }
    if (S.visible) renderer.render(scene, camera);
    if (dockOn) {
      placeCamera(S.cur.cam.to.az + window.scrollY * 0.0025, S.cur.cam.to.el, dockCam, 1.2);
      dockR.render(scene, dockCam);
    }
  }

  el.tabs.forEach((b, k) => b.addEventListener("click", () => switchTo(k, true)));
  if (cutBtn) cutBtn.addEventListener("click", toggleCut);
  if (window.ResizeObserver) new ResizeObserver(() => { resize(); if (reduce) renderOnce(); }).observe(host);
  else window.addEventListener("resize", () => { resize(); if (reduce) renderOnce(); });
  if (window.IntersectionObserver) {
    new IntersectionObserver((e) => { S.visible = e[0].isIntersecting; syncDock(); }, { threshold: 0.05 }).observe(stage);
  }

  resize();
  loadScene(0);
  if (reduce) renderOnce();
  else requestAnimationFrame(frame);
}
