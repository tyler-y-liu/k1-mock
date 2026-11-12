#!/usr/bin/env python3
"""把 index.html 的钟面动画导成 mp4（带配音）。

原理：页面里的动画完全由 stateAt(t) 这个纯函数决定，所以可以用
`index.html?t=<毫秒>` 定格任意一帧 → 逐帧截图 → ffmpeg 合成。

用法:
    python3 build-video.py                # 两个语言版都出
    python3 build-video.py --only zh      # 只出普通话
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TMP = Path("/tmp/clockvideo")
FPS = 15
ATEMPO = 1.15          # TTS 本身偏慢，提一点速更像真人
# TTS 句内停顿太长（一个停顿 0.5s），先掐头去尾，再把内部长静音压到 0.2s
AFILTER = ("aresample=44100,"
           "silenceremove=start_periods=1:start_threshold=-45dB:start_duration=0.02,"
           "areverse,"
           "silenceremove=start_periods=1:start_threshold=-45dB:start_duration=0.02,"
           "areverse,"
           f"atempo={ATEMPO},"
           "silenceremove=stop_periods=-1:stop_duration=0.2:stop_threshold=-40dB,"
           "apad=pad_dur=0.1")

VOICES = {
    "zh": ("Mandarin_治愈女声", "zh"),
    "yue": ("Cantonese_暖心师奶", "yue"),
}

LINES = {
    "zh": [
        "钟上这根短针指着九，就是九点。",
        "走到十点，走了一格。一格，就是一个小时。",
        "走到十一点，又走一格。两个小时。",
        "走到十二点，第三格。三个小时！",
        "九、十、十一、十二，是四个站牌。中间走过三段路，三个小时。",
    ],
    "yue": [
        "個鐘上面呢支短針指住九，就係九點。",
        "行到十點，行咗一格。一格，就係一個鐘。",
        "行到十一點，又行一格。兩個鐘。",
        "行到十二點，第三格。三個鐘！",
        "九、十、十一、十二，係四個站牌。中間行過三段路，三個鐘。",
    ],
}


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        sys.stderr.write(" ".join(map(str, cmd)) + "\n" + r.stdout + r.stderr)
        raise SystemExit(1)
    return r


def dur_of(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", str(path)]).stdout.strip()
    return float(out)


def tts(lang, text, out):
    voice, language = VOICES[lang]
    run([sys.executable, str(REPO / "scripts/mh.py"), "tts",
         "-o", str(out), "-v", voice, "--language", language, text])
    return out


def make_clips(lang):
    d = TMP / "audio" / lang
    d.mkdir(parents=True, exist_ok=True)
    clips = []
    for i, line in enumerate(LINES[lang], 1):
        raw = d / f"raw{i}.wav"
        fixed = d / f"{i}.wav"
        if not raw.exists():
            tts(lang, line, raw)
        # 统一采样率 + 提速 + 尾部留 150ms 静音，避免两句贴在一起
        run(["ffmpeg", "-y", "-v", "error", "-i", str(raw),
             "-filter:a", AFILTER, "-ac", "1", str(fixed)])
        clips.append(fixed)
    return clips, [dur_of(c) for c in clips]


def plan(dl, dy):
    """dl/dy: 两种语言各自的 5 段配音时长，取长的排时间轴（一套画面配两版音）"""
    d = [max(dl[i], dy[i]) for i in range(5)]
    START = int(d[0] * 1000) + 500
    STEP = max(int(max(d[1], d[2], d[3]) * 1000) + 500, 1900)
    TOTAL = START + 3 * STEP
    END = TOTAL + 200 + int(d[4] * 1000) + 1400
    return dict(START=START, SEG=1300, GAP=STEP - 1300, TOTAL=TOTAL, END=END)


def patch_timeline(plan_):
    p = HERE / "index.html"
    s = p.read_text()
    for key in ("START", "SEG", "GAP", "TOTAL", "END"):
        s, n = re.subn(rf"var {key}\b[^;]*;",
                       lambda m, k=key: f"var {k} = {plan_[k]};", s)
        assert n == 1, key
    p.write_text(s)
    print("时间轴:", plan_)


def capture_frames(plan_):
    fdir = TMP / "frames"
    if fdir.exists():
        shutil.rmtree(fdir)
    fdir.mkdir(parents=True)
    total = int(math.ceil(plan_["END"] / 1000 * FPS))
    url = (HERE / "index.html").as_uri()

    def one(k):
        t = int(k * 1000 / FPS)
        out = fdir / f"f_{k:05d}.png"
        run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--force-device-scale-factor=1", "--window-size=1280,800",
             f"--screenshot={out}", f"{url}?t={t}"])
        return out

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(one, range(total)))
    print(f"帧: {total} 张 ({FPS}fps, {plan_['END']/1000:.1f}s)")
    return fdir, total


def mix(lang, clips, plan_, out_mp4, fdir, total):
    T = [plan_["START"] + k * (plan_["SEG"] + plan_["GAP"]) for k in range(3)]
    at = [250, T[0], T[1], T[2], plan_["TOTAL"] + 200]
    args = ["ffmpeg", "-y", "-v", "error", "-framerate", str(FPS),
            "-i", str(fdir / "f_%05d.png")]
    for c in clips:
        args += ["-i", str(c)]
    fc = "".join(f"[{i+1}]adelay={at[i]}:all=1[a{i}];" for i in range(5))
    fc += "".join(f"[a{i}]" for i in range(5)) + \
          "amix=inputs=5:normalize=0:duration=longest,apad[aout]"
    args += ["-filter_complex", fc, "-map", "0:v", "-map", "[aout]",
             "-c:v", "libx264", "-preset", "slow", "-crf", "20",
             "-pix_fmt", "yuv420p", "-r", str(FPS),
             "-c:a", "aac", "-b:a", "128k",
             "-t", f"{plan_['END']/1000:.3f}", "-movflags", "+faststart", str(out_mp4)]
    run(args)
    print(f"✓ {out_mp4.name}  {dur_of(out_mp4):.1f}s  {out_mp4.stat().st_size/1e6:.2f}MB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["zh", "yue"])
    ap.add_argument("--skip-frames", action="store_true")
    a = ap.parse_args()
    langs = [a.only] if a.only else ["zh", "yue"]
    TMP.mkdir(parents=True, exist_ok=True)

    clips, durs = {}, {}
    for lg in ("zh", "yue"):
        clips[lg], durs[lg] = make_clips(lg)
        print(lg, [round(x, 2) for x in durs[lg]])

    p = plan(durs["zh"], durs["yue"])
    if not a.skip_frames:
        patch_timeline(p)
        fdir, total = capture_frames(p)
        json.dump(p, open(TMP / "plan.json", "w"))
    else:
        p = json.load(open(TMP / "plan.json"))
        fdir, total = TMP / "frames", len(list((TMP / "frames").glob("*.png")))

    names = {"zh": "clock-3h-mandarin.mp4", "yue": "clock-3h-cantonese.mp4"}
    for lg in langs:
        mix(lg, clips[lg], p, HERE / names[lg], fdir, total)


if __name__ == "__main__":
    main()
