// karaoke.js: brush-font karaoke with a character-by-character fill, drawn on the 2D compositor through
// window.overlayHook in core.js (crisp, above the paper grain). The bar covers y ≈ 975–1070 while a line shows.
//
// LY = [[start, end, "line", opts?]]. opts.sing = seconds the line takes to sing (for the fill); opts.pun =
// { from, to, at }: at time `at`, the characters `from` get a painted strike and `to` pops in above them (e.g. the fans'
// 大经理 → 大锦鲤 joke); opts.hold = seconds the line stays up after it ends, lifted above the next line.
// Times are clip times: the song's LRC time minus the clip start (see references/music-video.md). Let lines run into each other.
//
// ==== 本项目的唯一改动（一言片场）================================================
// 原文是写死的 `const LY = [ ...示例注释... ]`。这里改成读 window.KARAOKE_LY，
// 由 pipeline/painted.py 生成的 src/lyrics.js 注入。
// 为什么必须这样：LY 是 const，而且**下一行的 window.EXTRA_FONTS 在脚本加载时
// 就按 LY 的字符算好了字体预载集**——所以歌词数据必须先于本文件定义，
// 事后赋值既改不了 const、也来不及预载字体。
// 同步上游时若这行被还原，tests/test_pipeline.py 的「karaoke 读取注入歌词」会失败。
const LY = window.KARAOKE_LY || [
  // [3.02, 6.02, '第一句歌词'],
  // [6.07, 9.03, 'a line that holds', { sing: 2.2, hold: 1.4, pun: { from: 'old', to: 'new', at: 8.4 } }],
];
const KFONT = '"Ma Shan Zheng", "Kaiti SC", "STKaiti", serif';
// ==== 本项目的改动（一言片场）：字条位置与宽度不能再写死 ==========================
// 原文用 1024 作字条基线、960 作水平中心——那是按 1920×1080 标死的，
// 竖屏（1080×1920）会把字条画到画面上半部分、并且整体偏出右侧。
// 现在从 core.js 的 H / W 算，缺省值与原来完全一致（1080-56 = 1024、1920/2 = 960）。
const KBAR_Y = H - 56;
const KCX = W / 2;
window.EXTRA_FONTS = [[`64px ${KFONT}`, LY.map(l => l[2] + ((l[3] && l[3].pun && l[3].pun.to) || '')).join('')]];

window.overlayHook = (c, t) => {
  const i = LY.findIndex(l => t >= l[0] && t < l[1]);
  // a line with opts.hold stays up after its time, lifted and shrunk above the next one (two-row KTV style)
  const P = LY.find(l => l[3] && l[3].hold && t >= l[1] && t < l[1] + l[3].hold);
  if (P) { const a = t - P[1], k = ease(a / .15); kLine(c, P, t, KBAR_Y - 108 * k, 1 - .3 * k, 1 - ease((a - (P[3].hold - .3)) / .3), true); }
  if (i >= 0) kLine(c, LY[i], t, KBAR_Y, 1, 1, false);
};
function kLine(c, L, t, y, sc, alpha, held) {
  const [a, b, txt, opt = {}] = L;
  const grow = held ? 1 : easeOut((t - a) / .18) * (opt.hold ? 1 : 1 - ease((t - (b - .1)) / .1));
  if (grow < .02 || alpha <= .01) return;
  c.save(); c.globalAlpha = alpha; c.translate(KCX, y); c.scale(sc, sc); c.translate(-KCX, -y);
  const base = 64;
  c.font = `${base}px ${KFONT}`; c.textBaseline = 'middle'; c.textAlign = 'left';
  let total = c.measureText(txt).width;
  // 字条必须放得下：竖屏（W=1080）下长句会顶到画外。宁可缩字号，也不出画。
  const maxText = W - 200;
  if (total > maxText) {
    c.font = `${Math.max(26, Math.floor(base * maxText / total))}px ${KFONT}`;
    total = c.measureText(txt).width;
  }
  // the bar: a ragged ink lozenge that grows from the centre and boils with the linework
  const w = (total + 120) * grow, x0 = KCX - w / 2, y0 = y - 48, r = n => hash(BOILN * 13 + n) * 8 - 4;
  c.globalAlpha = .86 * alpha; c.fillStyle = PAL.ink; c.beginPath();
  c.moveTo(x0 + r(1), y0 + r(2)); c.lineTo(x0 + w / 2, y0 - 5 + r(3)); c.lineTo(x0 + w + r(4), y0 + r(5));
  c.lineTo(x0 + w + 16, y0 + 46); c.lineTo(x0 + w + r(6), y0 + 92 + r(7)); c.lineTo(x0 + w / 2, y0 + 96 + r(8));
  c.lineTo(x0 + r(9), y0 + 92 + r(10)); c.lineTo(x0 - 16, y0 + 46); c.closePath(); c.fill();
  c.globalAlpha = alpha;
  if (grow > .85) {
    // characters fill left to right as they're sung (opt.sing = how long the line takes to sing)
    const n = [...txt].length, singDur = opt.sing ?? Math.min((b - a) * .85, n * .3), f = held ? 1 : clamp((t - a) / singDur);
    const x = KCX - total / 2;
    c.fillStyle = PAL.cream; c.fillText(txt, x, y);
    c.save(); c.beginPath(); c.rect(x - 2, y - 50, total * f + 2, 100); c.clip(); c.fillStyle = PAL.ochre; c.fillText(txt, x, y); c.restore();
    if (opt.pun && t > opt.pun.at) pun(c, txt, x, y, opt.pun, t - opt.pun.at);
  }
  c.restore();
}

// Strike through `from` with a red brush stroke, then pop `to` in above it, tilted, in gold with an ink shadow.
function pun(c, txt, x, y, p, age) {
  const i = txt.indexOf(p.from); if (i < 0) return;
  const px = x + c.measureText(txt.slice(0, i)).width, pw = c.measureText(p.from).width;
  const k = easeOut(age / .25);
  c.save(); c.strokeStyle = '#D8394E'; c.lineCap = 'round'; c.lineWidth = 9;
  c.beginPath(); c.moveTo(px - 8, y + 6);
  for (let j = 1; j <= 8; j++) { const q = j / 8 * k; c.lineTo(px - 8 + (pw + 16) * q, y + 6 - 12 * q + Math.sin(j * 1.7) * 2); }
  c.stroke(); c.restore();
  const pop = backOut(clamp((age - .2) / .35)); if (pop <= .01) return;
  c.save(); c.translate(px + pw / 2 + 10, y - 88); c.rotate(-.12 + Math.sin(age * 14) * .04 * Math.exp(-age * 2)); c.scale(pop, pop);
  c.font = `96px ${KFONT}`; c.textAlign = 'center'; c.textBaseline = 'middle';
  c.lineJoin = 'round'; c.lineWidth = 14; c.strokeStyle = PAL.ink; c.strokeText(p.to, 0, 0);
  c.fillStyle = PAL.ink; c.fillText(p.to, 5, 6);
  c.fillStyle = '#F4B63A'; c.fillText(p.to, 0, 0);
  c.restore();
}
