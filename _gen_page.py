import json

P = {}

# 搜索框聚焦（配合 CDP 真键盘输入）
P["focus"] = r"""
(() => {
  const ins = [...document.querySelectorAll('input')].filter(e => e.offsetWidth || e.offsetHeight);
  const box = ins.find(i => /search/i.test((i.className || '') + (i.id || '') + (i.placeholder || ''))) || ins[0];
  if (!box) return false;
  box.focus();
  if (box.select) box.select();
  return true;
})()
"""

# 读出搜索结果行（酷狗把每首歌的参数挂在 jQuery data('song') 上）
P["rows"] = r"""
(() => {
  const out = [];
  const lis = [...document.querySelectorAll('li')].filter(li => li.querySelector('.song_name'));
  for (const li of lis) {
    const a = li.querySelector('.song_name');
    let d = null;
    try { d = window.jQuery ? jQuery(li).data('song') : null; } catch (e) {}
    if (!d) continue;
    out.push({ title: (a.title || a.textContent || '').trim().slice(0, 80),
               emix: d.EMixSongID || d.EMixSongId || '', songid: d.SongID || '',
               hash: d.FileHash || d.Hash || '' });
  }
  return out.slice(0, 40);
})()
"""

# 混音页：读播放状态
P["src"] = r"""
(() => {
  const m = document.getElementById('myAudio');
  const info = {};
  const n = document.querySelector('#songName');
  if (n) info.name = n.textContent.trim().slice(0, 60);
  if (!m) return { url: '', info: info, err: 'no-audio' };
  const url = m.currentSrc || m.src || '';
  return { url: url && !url.startsWith('about:') ? url : '',
           playing: !m.paused, t: +m.currentTime.toFixed(1), info: info,
           err: m.error ? m.error.code : null };
})()
"""

# 混音页：点播放按钮（页面上的 playbar-play / toggle）
P["click"] = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const cand = ['.icon-playbar-play', '#toggle', '.playbar-play', '[class*=playbar-play]']
    .map(s => document.querySelector(s)).find(e => e && !/pause/.test(e.className));
  if (!cand) {
    const m = document.getElementById('myAudio');
    if (m && !m.src) return { ok: false, why: 'no-button' };
    if (m) { try { await m.play(); } catch (e) {} return { ok: true, how: 'media.play' }; }
    return { ok: false, why: 'nothing' };
  }
  ['pointerover', 'mouseover'].forEach(t => cand.dispatchEvent(new MouseEvent(t, { bubbles: true })));
  await sleep(120);
  cand.click();
  return { ok: true, how: cand.className || cand.id };
})()
"""

# 判断当前是否真的在放
P["playing"] = r"""
(() => {
  const m = document.getElementById('myAudio') ||
            [...document.querySelectorAll('audio,video')].find(x => x.src && !x.src.includes('.html'));
  return { playing: !!(m && !m.paused && m.currentTime > 0),
           t: m ? +m.currentTime.toFixed(1) : 0,
           dur: m && m.duration ? +m.duration.toFixed(0) : 0 };
})()
"""

# 暂停：点播放器上的播放/暂停按钮
P["ctrl"] = r"""
(async () => {
  const CMD = %cmd%;
  const ms = [...document.querySelectorAll('audio,video')].filter(m => m.src && !m.src.includes('.html'));
  if (CMD === 'pause') {
    const t = document.querySelector('.icon-playbar-play, #toggle');
    if (t) t.click();
    ms.forEach(m => m.pause());
    return { ok: true, media: ms.length };
  }
  const b = document.querySelector('.icon-playbar-next, #next');
  if (b) { b.click(); return { ok: true }; }
  return { ok: false };
})()
"""

with open("page.js", "w", encoding="utf-8") as f:
    json.dump(P, f, ensure_ascii=False, indent=1)
print("page.js ok:", list(P))
