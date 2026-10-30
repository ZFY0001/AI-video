// 把 assets/icons/*.svg 转成**折线点**（p5.brush 只吃折线，没有 bezierVertex）。
//
// 和 svg2json.js 的区别：那个输出的是 M/L/C/Z 指令表（给 p5 原生 beginShape 用），
// 本项目实际走的是 p5.brush 的 beginShape/vertex/endShape，它只能连直线段，
// 所以曲线必须在这里**采样成点**，否则画出来是断的或者什么都画不出来。
//
//   node tools/svg2poly.js            # 打印可直接粘进 painted.py 的 ICON_POLYS
//   node tools/svg2poly.js --json     # 只输出 JSON
//
// 采样密度：三次贝塞尔每段固定 SEG 点。24×24 的图标放大到 167px 时，
// 每段 10 点约合 0.7px 的弦高误差，肉眼看不出折线感；再加点只会让 JS 变大。
const fs = require('fs');
const path = require('path');
const svgpath = require('svgpath');

const SEG = 10;                       // 每段三次贝塞尔的采样点数
const ICON_DIR = path.join(__dirname, '..', 'assets', 'icons');

const cubic = (p0, c1, c2, p1, t) => {
  const u = 1 - t;
  const a = u * u * u, b = 3 * u * u * t, c = 3 * u * t * t, d = t * t * t;
  return [a * p0[0] + b * c1[0] + c * c2[0] + d * p1[0],
          a * p0[1] + b * c1[1] + c * c2[1] + d * p1[1]];
};

const r2 = v => Math.round(v * 100) / 100;

// 一条 path 的 d → 若干条折线（每条折线是一串 [x,y]）
function flattenPath(d) {
  const s = svgpath(d).abs().unshort().unarc().toString();
  const toks = s.match(/[A-Za-z]|-?\d*\.?\d+(?:e-?\d+)?/g) || [];
  const polys = [];
  let cur = null, cx = 0, cy = 0, sx = 0, sy = 0, cmd = '', i = 0;
  const num = () => parseFloat(toks[i++]);
  const isCmd = t => /^[A-Za-z]$/.test(t);
  const push = p => { if (cur) cur.push([r2(p[0]), r2(p[1])]); };
  const flush = () => {
    // 单点折线没有意义（画不出线段），丢掉
    if (cur && cur.length >= 2) polys.push(cur);
    cur = null;
  };

  while (i < toks.length) {
    if (isCmd(toks[i])) cmd = toks[i++];
    if (cmd === 'M') {
      flush();
      cx = num(); cy = num(); sx = cx; sy = cy;
      cur = [];
      push([cx, cy]);
      cmd = 'L';
    } else if (cmd === 'L') {
      const nx = num(), ny = num();
      push([nx, ny]); cx = nx; cy = ny;
    } else if (cmd === 'H') {
      const nx = num();
      push([nx, cy]); cx = nx;
    } else if (cmd === 'V') {
      const ny = num();
      push([cx, ny]); cy = ny;
    } else if (cmd === 'C') {
      const x1 = num(), y1 = num(), x2 = num(), y2 = num(), x = num(), y = num();
      const p0 = [cx, cy];
      // 从 1 开始采样：t=0 就是上一个点，重复了会让笔刷在原地多点一下
      for (let k = 1; k <= SEG; k++) push(cubic(p0, [x1, y1], [x2, y2], [x, y], k / SEG));
      cx = x; cy = y;
    } else if (cmd === 'Q') {
      const qx = num(), qy = num(), x = num(), y = num();
      // 二次转三次，复用上面的采样
      const c1x = cx + (2 / 3) * (qx - cx), c1y = cy + (2 / 3) * (qy - cy);
      const c2x = x + (2 / 3) * (qx - x), c2y = y + (2 / 3) * (qy - y);
      const p0 = [cx, cy];
      for (let k = 1; k <= SEG; k++) push(cubic(p0, [c1x, c1y], [c2x, c2y], [x, y], k / SEG));
      cx = x; cy = y;
    } else if (cmd === 'Z' || cmd === 'z') {
      push([sx, sy]);                 // 闭合：补回起点
      flush();
      cx = sx; cy = sy;
    } else {
      i++;                            // 不认识的命令跳过，避免死循环
    }
  }
  flush();
  return polys;
}

const icons = {};
for (const f of fs.readdirSync(ICON_DIR).filter(f => f.endsWith('.svg')).sort()) {
  const name = path.basename(f, '.svg');
  const txt = fs.readFileSync(path.join(ICON_DIR, f), 'utf8');
  const ds = [...txt.matchAll(/<path[^>]*\sd="([^"]+)"/g)]
    .map(m => m[1].trim())
    // Tabler 的图标外面有一圈 M0 0h24v24H0z 的透明底，画出来是个方框，必须丢掉
    // 注意：先把空白去掉再比，所以模式串里也**不能有空格**（写成 'M0 0h24v24'
    // 会永远匹配不上，结果就是每个图标外面都多一个 24×24 的方框）。
    .filter(d => d && !d.replace(/\s/g, '').startsWith('M00h24v24'));
  const polys = [];
  for (const d of ds) {
    try { polys.push(...flattenPath(d)); } catch (e) { console.warn(`  跳过一条路径（${name}）: ${e.message}`); }
  }
  if (polys.length) icons[name] = polys;
}

if (process.argv.includes('--json')) {
  console.log(JSON.stringify(icons));
} else {
  console.log('const ICON_POLYS = {');
  for (const [k, v] of Object.entries(icons)) {
    console.log(`  ${JSON.stringify(k)}: ${JSON.stringify(v)},`);
  }
  console.log('};');
  console.error(`\n${Object.keys(icons).length} 个图标 / ` +
    `${Object.values(icons).reduce((a, v) => a + v.reduce((b, p) => b + p.length, 0), 0)} 个点`);
}
