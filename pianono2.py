import time
import argparse
import threading
import tkinter as tk
from tkinter import filedialog
from collections import defaultdict
from pathlib import Path
import io

import mido
import pydirectinput as di
import pygetwindow as gw
from pynput import keyboard

di.PAUSE = 0

WHITE_KEYS = list("1234567890qwertyuiopasdfghjklzxcvbnm")
DEGREE_ORDER = [0, 2, 4, 5, 7, 9, 11]
HAS_SHARP = {0, 2, 5, 7, 9}


def load_midifile_safe(path: str):
    # 1) обычная попытка
    try:
        return mido.MidiFile(path)
    except Exception:
        pass

    # 2) некоторые "mid" содержат MIDI не с 0 байта — ищем MThd
    try:
        data = Path(path).read_bytes()
        idx = data.find(b"MThd")
        if idx != -1:
            return mido.MidiFile(file=io.BytesIO(data[idx:]))
    except Exception:
        pass

    return None


def build_chromatic_keyboard(white_keys):
    chroma = []
    degree_i = 0
    last_i = len(white_keys) - 1
    for i, k in enumerate(white_keys):
        chroma.append((k, False))
        degree = DEGREE_ORDER[degree_i]
        if i != last_i and degree in HAS_SHARP:
            chroma.append((k, True))  # чёрная = Shift+эта же клавиша
        degree_i = (degree_i + 1) % 7
    return chroma


CHROMA_KEYS = build_chromatic_keyboard(WHITE_KEYS)


def pick_midi_file():
    root = tk.Tk()
    root.withdraw()
    return filedialog.askopenfilename(
        title="Выбери MIDI файл",
        filetypes=[("MIDI files", "*.mid *.midi"), ("All files", "*.*")]
    )


def bring_roblox_to_front():
    titles = [t for t in gw.getAllTitles() if "roblox" in t.lower()]
    if not titles:
        return
    w = gw.getWindowsWithTitle(titles[0])[0]
    try:
        w.activate()
    except Exception:
        pass


def parse_hotkey(name: str):
    name = name.strip().lower()
    if len(name) == 1:
        return keyboard.KeyCode.from_char(name)

    aliases = {
        "esc": keyboard.Key.esc,
        "escape": keyboard.Key.esc,
        "space": keyboard.Key.space,
        "enter": keyboard.Key.enter,
        "return": keyboard.Key.enter,
        "tab": keyboard.Key.tab,
    }
    if name in aliases:
        return aliases[name]

    if name.startswith("f") and name[1:].isdigit():
        fnum = int(name[1:])
        k = getattr(keyboard.Key, f"f{fnum}", None)
        if k is not None:
            return k

    raise ValueError(f"Не понимаю клавишу: {name}")


def wait_for_key(key_name: str):
    target = parse_hotkey(key_name)

    def on_press(k):
        if k == target:
            return False

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()


def stop_listener(stop_key: str, stop_event: threading.Event):
    target = parse_hotkey(stop_key)

    def on_press(k):
        if k == target:
            stop_event.set()
            return False

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()


def midi_to_note_events(path: str, channels=None):
    mid = load_midifile_safe(path)
    if mid is None:
        raise ValueError("Bad/unsupported MIDI file")

    merged = mido.merge_tracks(mid.tracks)

    tempo = 500000
    tpq = mid.ticks_per_beat
    t_sec = 0.0

    active = defaultdict(list)  # (ch, note) -> [(start, vel), ...]
    out = []

    for msg in merged:
        t_sec += mido.tick2second(msg.time, tpq, tempo)

        if msg.type == "set_tempo":
            tempo = msg.tempo
            continue

        ch = getattr(msg, "channel", 0)
        if ch == 9:  # drums
            continue
        if channels is not None and ch not in channels:
            continue

        if msg.type == "note_on" and msg.velocity > 0:
            active[(ch, msg.note)].append((t_sec, msg.velocity))
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            stack = active.get((ch, msg.note))
            if stack:
                start, vel = stack.pop()
                end = t_sec
                if end > start:
                    out.append((start, end, msg.note, vel, ch))

    for (ch, note), stack in active.items():
        for start, vel in stack:
            out.append((start, start + 0.10, note, vel, ch))

    out.sort(key=lambda x: x[0])
    return out


def extract_melody(note_events, strategy="smooth", stickiness=1.2, min_dur=0.04):
    actions = []
    for s, e, n, v, ch in note_events:
        actions.append((s, 1, n, v))  # on
        actions.append((e, 0, n, v))  # off
    actions.sort(key=lambda x: (x[0], x[1]))  # off раньше on

    active = {}
    cur_note = None
    cur_vel = 80
    cur_start = None
    prev_note = None

    melody = []

    def pick_note():
        nonlocal prev_note, cur_note
        if not active:
            return None, 0

        if strategy == "highest":
            n = max(active.keys())
            return n, active[n]

        best = None
        for n, v in active.items():
            jump = 0 if prev_note is None else abs(n - prev_note)
            switch_pen = stickiness if (cur_note is not None and n != cur_note) else 0.0
            score = (0.55 * n) + (0.25 * v) - (1.35 * jump) - (8.0 * switch_pen)
            cand = (score, n, v)
            if best is None or cand > best:
                best = cand
        _, n, v = best
        return n, v

    for i in range(len(actions) - 1):
        t, kind, n, v = actions[i]

        if kind == 0:
            active.pop(n, None)
        else:
            active[n] = v

        n_pick, v_pick = pick_note()

        if n_pick is None:
            if cur_note is not None and cur_start is not None:
                if t - cur_start >= min_dur:
                    melody.append((cur_start, t, cur_note, cur_vel))
                cur_note = None
                cur_start = None
            continue

        if cur_note != n_pick:
            if cur_note is not None and cur_start is not None:
                if t - cur_start >= min_dur:
                    melody.append((cur_start, t, cur_note, cur_vel))
            cur_note = n_pick
            cur_vel = v_pick
            cur_start = t
            prev_note = n_pick

    last_t = actions[-1][0]
    if cur_note is not None and cur_start is not None and last_t - cur_start >= min_dur:
        melody.append((cur_start, last_t, cur_note, cur_vel))

    merged = []
    for s, e, n, v in melody:
        if merged and merged[-1][2] == n and s - merged[-1][1] < 0.03:
            merged[-1] = (merged[-1][0], e, n, max(merged[-1][3], v))
        else:
            merged.append((s, e, n, v))

    return merged  # (s,e,n,v)


def best_base_and_transpose(notes, base_candidates, tr_min=-36, tr_max=36):
    if not notes:
        return base_candidates[0], 0

    nkeys = len(CHROMA_KEYS)
    best = None

    for base in base_candidates:
        top = base + (nkeys - 1)
        for tr in range(tr_min, tr_max + 1):
            shifted = [n + tr for n in notes]
            in_mask = [(base <= x <= top) for x in shifted]
            in_count = sum(in_mask)
            out_count = len(shifted) - in_count
            if in_count == 0:
                continue

            idxs = [shifted[i] - base for i, ok in enumerate(in_mask) if ok]
            edge_pen = 0.0
            for idx in idxs:
                edge = min(idx, (nkeys - 1) - idx)
                edge_pen += max(0.0, 3 - edge)

            score = in_count - 0.75 * out_count - 0.12 * edge_pen - 0.05 * abs(tr)
            cand = (score, in_count, -out_count, -abs(tr), base, tr)
            if best is None or cand > best:
                best = cand

    if best is None:
        return base_candidates[0], 0
    return best[4], best[5]


def note_to_key(note_midi: int, base_midi: int):
    idx = note_midi - base_midi
    if 0 <= idx < len(CHROMA_KEYS):
        return CHROMA_KEYS[idx]  # (key, use_shift)
    return None


def build_actions(note_events, min_hold=0.055, max_hold=0.35):
    actions = []
    for s, e, n, v in note_events:
        dur = max(min_hold, min(max_hold, e - s))
        e2 = s + dur
        actions.append((s, 1, n))
        actions.append((e2, 0, n))
    actions.sort(key=lambda x: (x[0], x[1]))
    return actions


def key_down(key: str, use_shift: bool):
    # ВАЖНО: shift держим только на момент keyDown чёрной ноты
    if use_shift:
        di.keyDown("shift")
        di.keyDown(key)
        di.keyUp("shift")
    else:
        di.keyDown(key)


def key_up(key: str, use_shift: bool):
    di.keyUp(key)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=str, default="")
    ap.add_argument("--start-key", type=str, default="f8")
    ap.add_argument("--stop-key", type=str, default="f9")
    ap.add_argument("--speed", type=float, default=1.0, help=">1 медленнее, <1 быстрее")

    ap.add_argument("--mode", choices=["melody", "full"], default="melody")
    ap.add_argument("--melody-strategy", choices=["smooth", "highest"], default="smooth")
    ap.add_argument("--stickiness", type=float, default=1.2)

    ap.add_argument("--max-poly", type=int, default=3)
    ap.add_argument("--min-hold", type=float, default=0.055)
    ap.add_argument("--max-hold", type=float, default=0.35)

    ap.add_argument("--base-midi", type=int, default=9999)
    ap.add_argument("--transpose", type=int, default=9999)

    ap.add_argument("--channels", type=str, default="all")

    args = ap.parse_args()

    path = args.file.strip() or pick_midi_file()
    if not path:
        print("Файл не выбран.")
        return

    ch_filter = None
    if args.channels.strip().lower() != "all":
        ch_filter = set(int(x.strip()) for x in args.channels.split(",") if x.strip().isdigit())

    try:
        raw = midi_to_note_events(path, channels=ch_filter)
    except Exception as e:
        print(f"Не смог прочитать MIDI: {e}")
        return

    if not raw:
        print("Не нашёл нот в MIDI.")
        return

    if args.mode == "melody":
        mel = extract_melody(raw, strategy=args.melody_strategy, stickiness=args.stickiness)
        note_events = [(s, e, n, v) for (s, e, n, v) in mel]
        max_poly = 1
    else:
        note_events = [(s, e, n, v) for (s, e, n, v, ch) in raw]
        max_poly = max(1, int(args.max_poly))

    notes = [n for (_, _, n, _) in note_events]
    base_candidates = [24, 36, 48, 60]
    auto_base, auto_tr = best_base_and_transpose(notes, base_candidates)

    base_midi = auto_base if args.base_midi == 9999 else int(args.base_midi)
    transpose = auto_tr if args.transpose == 9999 else int(args.transpose)

    note_events = [(s, e, n + transpose, v) for (s, e, n, v) in note_events]
    actions = build_actions(note_events, min_hold=args.min_hold, max_hold=args.max_hold)

    top_midi = base_midi + (len(CHROMA_KEYS) - 1)
    total_on = sum(1 for (_, kind, _) in actions if kind == 1)
    in_range = sum(1 for (_, kind, n) in actions if kind == 1 and note_to_key(n, base_midi))

    print("\n=== READY ===")
    print("\nScript by Skufupanda")
    print(f"file        : {path}")
    print(f"mode        : {args.mode}")
    print(f"notes       : {total_on} | in_range: {in_range}/{total_on}")
    print(f"base_midi   : {base_midi}  (range {base_midi}..{top_midi})")
    print(f"transpose   : {transpose} semitones")
    print(f"hold        : {args.min_hold:.3f}..{args.max_hold:.3f}s")
    print(f"speed       : {args.speed:.2f}")
    print("================\n")

    print("Roblox: Windowed/Windowed Fullscreen → кликни по пианино → ENG раскладка.")
    bring_roblox_to_front()
    print(f"Start: {args.start_key.upper()} | Stop: {args.stop_key.upper()}")

    wait_for_key(args.start_key)

    stop_event = threading.Event()
    threading.Thread(target=stop_listener, args=(args.stop_key, stop_event), daemon=True).start()

    pressed = {}  # note -> (key, use_shift)
    pressed_count = 0
    start_wall = time.time()

    try:
        di.press("/")
        time.sleep(0.1)
        di.write("Script by Skufupanda")
        time.sleep(0.1)
        di.press("enter")

        for (t_action, kind, note) in actions:
            if stop_event.is_set():
                break

            target = start_wall + (t_action * args.speed)
            now = time.time()
            if target > now:
                time.sleep(target - now)

            mapped = note_to_key(note, base_midi)
            if not mapped:
                continue
            key, use_shift = mapped

            if kind == 1:  # ON
                if note in pressed:
                    continue
                if pressed_count >= max_poly:
                    continue

                key_down(key, use_shift)
                pressed[note] = (key, use_shift)
                pressed_count += 1

            else:  # OFF
                if note not in pressed:
                    continue
                k, sh = pressed.pop(note)
                key_up(k, sh)
                pressed_count = max(0, pressed_count - 1)

    finally:
        for note, (k, sh) in list(pressed.items()):
            key_up(k, sh)

    print("DONE.")


if __name__ == "__main__":
    main()