"""WriteAlong v2 — desktop dictation GUI (tkinter).

Run:  python3 dictation_gui.py [file.pdf|file.txt]

- File picker + drag & drop (.txt / .pdf)
- Current-page text + scanned page image, click any word to jump
- v2 pause engine: think + write x length-curve + syllables/caps/digits + punctuation, x pace
- Pace modifier, presets, handwriting calibration, hold-to-extend, sentence loop
- Offline TTS via your Piper model (en_US-lessac-medium.onnx), audio via pygame
"""
import io
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import wave
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAVE_DND = True
except ImportError:
    HAVE_DND = False

try:
    import pymupdf  # page images + text
    HAVE_MUPDF = True
except ImportError:
    HAVE_MUPDF = False

from pypdf import PdfReader
import pygame
from piper import PiperVoice

try:
    from PIL import Image, ImageTk
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

# ---------------- v2 gap engine (same as index.html) ----------------
COMMON = set(
    "the be to of and a in that have i it for not on with he as you do at this but his by "
    "from they we say her she or an will my one all would there their what so up out if about "
    "who get which go me when make can like time no just him know take people into year your "
    "good some could them see other than then now look only come its over think also back after "
    "use two how our work first well way even new want because any these give day most us is are "
    "was were had has".split()
)
PRESETS = {
    "beginner": dict(spl=0.70, think=0.90, pace=1.15, sent=2.0),
    "classroom": dict(spl=0.55, think=0.60, pace=1.0, sent=1.6),
    "exam": dict(spl=0.38, think=0.40, pace=0.85, sent=1.2),
}
MIN_GAP, MAX_GAP = 1.1, 12.0
CALIB_WORDS = ["cat", "water", "school", "beautiful", "extraordinary"]

# ---------------- Material Design 3 (dark, baseline) ----------------
# Applied per Google's Material guidelines: tonal color roles, 8dp spacing
# rhythm, title/body/label type scale, pill filled + tonal buttons, flat
# cards on surface-container, linear progress in primary.
M3 = dict(
    surface="#131318", container_low="#1A1B20", container="#1E1F25",
    container_high="#282A31", highest="#36343B",
    primary="#D0BCFF", on_primary="#381E72",
    secondary_container="#4A4458", on_secondary_container="#E8DEF8",
    on_surface="#E5E1E9", variant="#CAC4D0", outline="#938F99",
    outline_variant="#49454F", error="#F2B8B5",
)

# ---------------- punctuation narration ----------------
# Tokens that strip down to nothing (",", ".", "—", "___") must still be
# *heard*: map them to spoken names so dictation never goes silent.
PUNCT_NAMES = {
    ".": "period", ",": "comma", ";": "semicolon", ":": "colon",
    "!": "exclamation mark", "?": "question mark", "…": "ellipsis",
    "—": "em dash", "–": "en dash", "-": "hyphen", "_": "blank",
    "(": "open parenthesis", ")": "close parenthesis",
    "[": "open bracket", "]": "close bracket",
    "{": "open brace", "}": "close brace",
    '"': "quote", "'": "apostrophe", "“": "quote", "”": "quote",
    "/": "slash", "\\": "backslash", "@": "at sign", "&": "and sign",
    "*": "asterisk", "#": "hash", "%": "percent", "$": "dollar",
    "+": "plus", "=": "equals",
}


def spoken_form(tok):
    """What the narrator should SAY for a token. Pure-punctuation tokens
    (',', '.', '—', '___' blanks…) come back as their spoken name so they
    are never silently skipped. Returns '' only for unpronounceable junk."""
    c = clean_word(tok)
    if c:
        return c
    chars = [ch for ch in (tok or "") if not ch.isspace()]
    if not chars:
        return ""
    if len(set(chars)) == 1 and len(chars) > 2:
        if set(chars) == {"."}:
            return "ellipsis"
        return PUNCT_NAMES.get(chars[0], "")  # "___" -> "blank"
    if set(chars) <= {"."}:
        return "ellipsis" if len(chars) > 1 else "period"
    names = [PUNCT_NAMES.get(ch, "") for ch in chars]
    return " ".join(n for n in names if n)


def clean_word(w):
    return re.sub(r"[^A-Za-z0-9]", "", w or "")


def syllables(word):
    word = re.sub(r"[^a-z]", "", word.lower())
    if not word or len(word) <= 3:
        return 1
    groups = re.findall(r"[aeiouy]+", re.sub(r"e$", "", word))
    return max(1, min(6, len(groups) or 1))


class GapEngine:
    def __init__(self, spl=0.55, think=0.60, pace=1.0, sent=1.6):
        self.spl, self.think, self.pace, self.sent = spl, think, pace, sent

    def breakdown(self, display, first_of_sentence=False, last_of_page=False):
        clean = clean_word(display)
        L = len(clean) or 1
        syl = syllables(clean)
        curve = 1 + max(0, L - 6) * 0.08
        write = L * self.spl * curve
        if L > 7 and clean.lower() not in COMMON:
            write *= 1.15
        syl_extra = max(0, syl - 1) * 0.12
        extra = 0.0
        tags = []
        if re.search(r"\d", display):
            extra += 0.5; tags.append("digit+0.5")
        if len(clean) > 1 and clean == clean.upper() and re.search(r"[A-Z]", clean):
            extra += 0.4; tags.append("CAPS+0.4")
        if re.search(r"[-'’]", display) or re.search(r"[a-z][A-Z]", clean):
            extra += 0.3; tags.append("hyphen/case+0.3")
        punct = 0.0
        if re.search(r"[,;:]$", display):
            punct += 0.8; tags.append("clause+0.8")
        if re.search(r"[.?!…]$", display):
            punct += self.sent; tags.append(f"sentence+{self.sent:.1f}")
        if first_of_sentence:
            punct += 0.4; tags.append("lead+0.4")
        if last_of_page:
            punct += 0.6; tags.append("page-end+0.6")
        raw = self.think + write + syl_extra + extra + punct
        total = min(MAX_GAP, max(MIN_GAP, raw * self.pace))
        return dict(L=L, syl=syl, curve=curve, think=self.think, write=write,
                    syl_extra=syl_extra, extra=extra, punct=punct,
                    tags=tags, raw=raw, total=total)


# ---------------- document ----------------
class Doc:
    def __init__(self):
        self.name = ""
        self.pages = []   # list of {"text": str, "words": [str]}
        self.flat = []    # list of {"display": str, "page": int}
        self.pixmaps = {}  # page -> PIL image (pdf only)

    def load(self, path):
        self.__init__()
        self.name = os.path.basename(path)
        ext = path.rsplit(".", 1)[-1].lower()
        if ext == "pdf":
            texts = []
            try:
                if HAVE_MUPDF:
                    doc = pymupdf.open(path)
                    for p in doc:
                        texts.append(p.get_text())
                else:
                    texts = [(pg.extract_text() or "") for pg in PdfReader(path).pages]
            except Exception:
                texts = [(pg.extract_text() or "") for pg in PdfReader(path).pages]
            for t in texts:
                self.pages.append({"text": t, "words": t.split()})
            # prerender page images lazily; store path for lazy render
            self._pdf_path = path
        elif ext == "txt":
            raw = open(path, encoding="utf-8", errors="ignore").read()
            chunks = [c.strip() for c in raw.split("\f") if c.strip()]
            if len(chunks) <= 1:
                w = raw.split(); chunks = [" ".join(w[i:i + 220]) for i in range(0, len(w), 220)] or [""]
            for c in chunks:
                self.pages.append({"text": c, "words": c.split()})
            self._pdf_path = None
        else:
            raise ValueError("Only .txt / .pdf supported")
        for pi, p in enumerate(self.pages):
            for w in p["words"]:
                self.flat.append({"display": w, "page": pi})

    def page_image(self, pi, zoom=1.3, max_w=520):
        """Returns a tk.PhotoImage for page pi (no PIL needed on Tk 9: PNG via temp file)."""
        if getattr(self, "_pdf_path", None) and HAVE_MUPDF:
            import tempfile
            doc = pymupdf.open(self._pdf_path)
            pix = doc[pi].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
            if HAVE_PIL:
                key = (pi, zoom)
                if key not in self.pixmaps:
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    if img.width > max_w:
                        img = img.resize((max_w, int(img.height * max_w / img.width)))
                    self.pixmaps[key] = img
                return ("pil", self.pixmaps[key])
            path = os.path.join(tempfile.gettempdir(), f"writealong_p{pi}.png")
            pix.save(path)
            return ("file", path)
        return None


# ---------------- app ----------------
class App:
    def __init__(self, root, model_path, start_file=None):
        self.root = root
        self.doc = Doc()
        self.engine = GapEngine()
        self.cursor = 0
        self.playing = False
        self.looping = False
        self.gen = 0          # bump to abort worker on seek/stop
        self.gap_end = 0.0
        self.gap_total = 1.0
        self.rate = 0.92
        self.repeats = 1
        self.spell = False
        self.double_say = True
        self.blind = False
        self.show_scan = True
        self.cache = {}
        self.photo = None
        self.ui_q = queue.Queue()  # worker thread -> main thread UI calls

        try:
            self.voice = PiperVoice.load(model_path)
        except Exception as e:
            messagebox.showerror("Voice model", f"Could not load {model_path}:\n{e}")
            raise SystemExit(1)
        pygame.mixer.init(frequency=22050)

        self.build_ui()
        if start_file:
            self.open_path(start_file)

    # ----- TTS -----
    def synth(self, text):
        if text not in self.cache:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as f:
                self.voice.synthesize_wav(text, f)
            self.cache[text] = buf.getvalue()
        return self.cache[text]

    def play_wav(self, data, gen):
        snd = pygame.mixer.Sound(file=io.BytesIO(data))
        snd.play()
        while pygame.mixer.get_busy():
            if gen != self.gen or not self.playing:
                snd.stop()
                return False
            time.sleep(0.02)
        return True

    def say(self, text, gen, rate=None):
        return self.play_wav(self.synth(text), gen)

    # ----- gap helpers -----
    def opts(self, i):
        f = self.doc.flat
        first = i == 0 or bool(re.search(r"[.?!…]$", f[i - 1]["display"]))
        last = i == len(f) - 1 or f[i + 1]["page"] != f[i]["page"]
        return first, last

    def gap_total_for(self, i):
        first, last = self.opts(i)
        return self.engine.breakdown(self.doc.flat[i]["display"], first, last)["total"]

    # ----- worker -----
    def worker(self):
        gen = self.gen
        n = len(self.doc.flat)
        while self.playing and gen == self.gen and self.cursor < n:
            i = self.cursor
            tok = self.doc.flat[i]["display"]
            spoken = spoken_form(tok)
            if not spoken:
                # unpronounceable junk (stray symbols): keep rhythm with a short
                # beat instead of going silent, then move on
                self.set_status(f'⋯ "{tok}" (symbol — brief pause)')
                self.gap_total = max(1.1, 0.8 * self.engine.pace)
                self.gap_end = time.time() + self.gap_total
                while time.time() < self.gap_end:
                    if gen != self.gen or not self.playing:
                        break
                    time.sleep(0.05)
                else:
                    self.cursor = i + 1
                continue
            clean_len = len(clean_word(tok))
            self.ui(self.refresh)
            self.set_status(f'🔊 Saying "{tok}"…')
            time.sleep(0.18)
            if gen != self.gen or not self.playing:
                break
            times = max(self.repeats, 2 if (self.double_say and clean_len >= 9) else 1)
            ok = True
            for k in range(times):
                if not self.say(spoken, gen):
                    ok = False; break
                if times > 1:
                    time.sleep(0.25)
            if not ok or gen != self.gen or not self.playing:
                break
            if self.spell and clean_len >= 8:
                self.set_status(f'🔤 Spelling "{tok}"…')
                if not self.say(" ".join(spoken), gen):
                    break
            gap = self.gap_total_for(i)
            b = self.engine.breakdown(tok, *self.opts(i))
            self.set_status(f'✍ Write "{tok}" — {gap:.2f}s   (think {b["think"]:.2f} + write {b["write"]:.2f} + punct {b["punct"]:.2f} ×{self.engine.pace:.2f})')
            self.gap_total = gap
            self.gap_end = time.time() + gap
            while time.time() < self.gap_end:
                if gen != self.gen or not self.playing:
                    break
                time.sleep(0.05)
            else:
                if self.looping:
                    s, e = self.sentence_bounds(i)
                    self.cursor = s if i >= e else i + 1
                else:
                    self.cursor = i + 1
                continue
            break
        if gen == self.gen and self.cursor >= len(self.doc.flat) and self.doc.flat:
            self.set_status("✅ Done!")
        self.ui(self.on_worker_end)

    def on_worker_end(self):
        if self.playing and self.cursor >= len(self.doc.flat):
            self.playing = False
            self.play_btn.config(text="▶ Play")
        self.refresh()

    # ----- transport -----
    def play(self):
        if not self.doc.flat:
            messagebox.showinfo("No document", "Open a .txt or .pdf first (or load the demo).")
            return
        if self.cursor >= len(self.doc.flat) - 1:
            self.cursor = 0
        try:
            pygame.mixer.stop()
        except Exception:
            pass
        self.playing = True
        self.gen += 1
        self.play_btn.config(text="❚❚ Pause")
        threading.Thread(target=self.worker, daemon=True).start()
        self.tick_progress()

    def pause(self):
        self.gen += 1
        self.playing = False
        try:
            pygame.mixer.stop()
        except Exception:
            pass
        self.play_btn.config(text="▶ Play")
        self.set_status("Paused")
        self.refresh()

    def toggle(self):
        self.pause() if self.playing else self.play()

    def jump(self, i):
        if not self.doc.flat:
            return
        was = self.playing
        self.gen += 1
        self.playing = False
        try:
            pygame.mixer.stop()
        except Exception:
            pass
        self.cursor = max(0, min(len(self.doc.flat) - 1, i))
        self.play_btn.config(text="▶ Play")
        self.refresh()
        if was:
            self.play()

    def sentence_bounds(self, i):
        f = self.doc.flat
        s, e = i, i
        while s > 0 and not re.search(r"[.!?…:]$", f[s - 1]["display"]):
            s -= 1
        while e < len(f) - 1 and not re.search(r"[.!?…:]$", f[e]["display"]):
            e += 1
        return s, e

    def hold(self):
        if self.playing:
            self.gap_end += 2.5
            self.set_status(self.status_var.get() + "  (+2.5s held ✋)")

    # ----- file -----
    def open_dialog(self):
        p = filedialog.askopenfilename(filetypes=[("Text/PDF", "*.txt *.pdf"), ("All", "*.*")])
        if p:
            self.open_path(p)

    def open_path(self, path):
        try:
            self.pause()
            self.doc.load(path)
            self.cursor = 0
            self.seek.config(to=max(0, len(self.doc.flat) - 1))
            self.file_var.set(f"{self.doc.name} — {len(self.doc.flat)} words, {len(self.doc.pages)} pages")
            self.build_pages()
            self.refresh()
            self.set_status("Ready — press Play.")
        except Exception as e:
            messagebox.showerror("Open failed", str(e))

    def load_demo(self):
        self.pause()
        self.doc.__init__()
        self.doc.name = "demo.txt"
        for t in ["Dictation demo. Press Play. Short words like cat pass quickly, but a monster like extraordinary automatically earns extra time — plus more at commas, and full stops.",
                  "Tune everything with My pace. Hit Hold when your pen lags. Spell mode spells hard words aloud, letter by letter."]:
            self.doc.pages.append({"text": t, "words": t.split()})
        for pi, p in enumerate(self.doc.pages):
            for w in p["words"]:
                self.doc.flat.append({"display": w, "page": pi})
        self.doc._pdf_path = None
        self.cursor = 0
        self.seek.config(to=max(0, len(self.doc.flat) - 1))
        self.file_var.set(f"demo.txt — {len(self.doc.flat)} words, {len(self.doc.pages)} pages")
        self.build_pages()
        self.refresh()

    # ----- UI -----
    def apply_material(self):
        """Google Material Design 3 (dark baseline): tonal roles, 8dp rhythm,
        title/body/label type scale, filled + tonal pill buttons, flat cards
        on surface-container, primary linear progress."""
        self.root.configure(bg=M3["surface"])
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        F = dict(font=("Sans", 10))
        st.configure("TFrame", background=M3["surface"])
        st.configure("Card.TFrame", background=M3["container"], padding=16)
        st.configure("TLabel", background=M3["surface"], foreground=M3["on_surface"], **F)
        st.configure("Card.TLabel", background=M3["container"], foreground=M3["on_surface"], **F)
        st.configure("Dim.TLabel", background=M3["surface"], foreground=M3["variant"], font=("Sans", 9))
        st.configure("Section.TLabel", background=M3["surface"], foreground=M3["variant"],
                     font=("Sans", 10, "bold"))
        st.configure("Display.TLabel", background=M3["surface"], foreground=M3["on_surface"],
                     font=("Sans", 40, "bold"))
        # M3 filled button (primary action) + tonal buttons
        st.configure("Filled.TButton", background=M3["primary"], foreground=M3["on_primary"],
                     borderwidth=0, focusthickness=0, padding=(24, 12), font=("Sans", 11, "bold"))
        st.map("Filled.TButton", background=[("active", "#E4D8FF"), ("pressed", "#B79DF5")])
        st.configure("Tonal.TButton", background=M3["secondary_container"],
                     foreground=M3["on_secondary_container"],
                     borderwidth=0, focusthickness=0, padding=(14, 9), **F)
        st.map("Tonal.TButton",
               background=[("active", "#5B5566"), ("pressed", "#3E3A47")])
        st.configure("TCheckbutton", background=M3["surface"], foreground=M3["on_surface"], **F)
        st.map("TCheckbutton", background=[("active", M3["surface"])])
        st.configure("TEntry", fieldbackground=M3["container_high"], foreground=M3["on_surface"],
                     borderwidth=0, padding=8)
        st.configure("TSpinbox", fieldbackground=M3["container_high"], foreground=M3["on_surface"],
                     borderwidth=0, padding=6)
        # M3 slider: primary thumb on surface-container-highest track
        st.configure("Horizontal.TScale", background=M3["surface"], troughcolor=M3["highest"],
                     sliderthickness=18, borderwidth=0)
        # M3 linear progress: primary on surface-container-highest
        st.configure("Horizontal.TProgressbar", background=M3["primary"],
                     troughcolor=M3["highest"], borderwidth=0, thickness=6)
        st.configure("TPanedwindow", background=M3["surface"])
        st.configure("Sash", background=M3["outline_variant"], sashthickness=1)

    def fix_space_key(self):
        """Space must ALWAYS mean play/pause — never re-trigger whatever
        button was last clicked. ttk buttons fire on Space via class
        bindings that run before toplevel bindings, so: strip Space off
        every button-like class, drop them from Tab focus, and handle
        Space globally (except while typing in a text field)."""
        for cls in ("Button", "TButton", "Checkbutton", "TCheckbutton",
                    "Radiobutton", "TRadiobutton", "Spinbox", "TSpinbox"):
            for seq in ("<Key-space>", "<KeyPress-space>", "<KeyRelease-space>"):
                try:
                    self.root.unbind_class(cls, seq)
                except Exception:
                    pass
        for w in self._all_widgets(self.root):
            if isinstance(w, (ttk.Button, ttk.Checkbutton, ttk.Scale, ttk.Progressbar)):
                try:
                    w.configure(takefocus=False)
                except Exception:
                    pass
        self.root.bind_all("<KeyPress-space>", self._on_space, add="+")

    @staticmethod
    def _all_widgets(root):
        out = [root]
        for w in root.winfo_children():
            out += App._all_widgets(w)
        return out

    def _on_space(self, e):
        w = self.root.focus_get()
        try:
            cls = (w.winfo_class() if w else "") or ""
        except Exception:
            cls = ""
        if cls in ("Entry", "TEntry", "Text", "Spinbox", "TSpinbox"):
            return None  # typing — leave Space alone
        self.toggle()
        return "break"

    def build_ui(self):
        self.root.title("WriteAlong v2 — Dictation Studio")
        self.root.geometry("1240x760")
        self.apply_material()
        self.pump()  # drain worker->UI queue in main thread
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        ttk.Button(top, text="📂 Open file", command=self.open_dialog).pack(side="left")
        ttk.Button(top, text="Try demo", command=self.load_demo).pack(side="left", padx=6)
        self.file_var = tk.StringVar(value="No file — open a .txt/.pdf or try demo (drag & drop works too)")
        ttk.Label(top, textvariable=self.file_var, foreground="gray").pack(side="left", padx=10)
        if HAVE_DND:
            try:
                self.root.drop_target_register(DND_FILES)
                self.root.dnd_bind("<<Drop>>", lambda e: self.open_path(self.root.tk.splitlist(e.data)[0]))
            except Exception:
                pass  # plain Tk root (e.g. tests) — file picker still works

        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=4)

        # left: pages + search
        left = ttk.Frame(body, padding=6)
        body.add(left, weight=1)
        ttk.Label(left, text="PAGES — click to skip").pack(anchor="w")
        self.search_var = tk.StringVar()
        se = ttk.Entry(left, textvariable=self.search_var)
        se.pack(fill="x", pady=4)
        se.bind("<Return>", lambda e: self.find())
        self.page_list = tk.Listbox(left, height=18)
        self.page_list.pack(fill="both", expand=True)
        self.page_list.bind("<<ListboxSelect>>", self.on_page_select)

        # center: reader
        center = ttk.Frame(body, padding=6)
        body.add(center, weight=3)
        self.big_var = tk.StringVar(value="—")
        ttk.Label(center, textvariable=self.big_var, font=("TkDefaultFont", 40, "bold"), anchor="center").pack(fill="x")
        self.ctx_var = tk.StringVar(value="Open a file, then press Play (Space).")
        ttk.Label(center, textvariable=self.ctx_var, wraplength=560, anchor="center", justify="center").pack(fill="x")
        self.bd_var = tk.StringVar(value="Pause breakdown appears here.")
        ttk.Label(center, textvariable=self.bd_var, wraplength=600, foreground="gray", font=("TkDefaultFont", 9)).pack(fill="x", pady=2)

        prog = ttk.Frame(center)
        prog.pack(fill="x", pady=4)
        self.prog = ttk.Progressbar(prog, maximum=100)
        self.prog.pack(fill="x")
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(center, textvariable=self.status_var, foreground="gray").pack(fill="x")

        mid = ttk.Panedwindow(center, orient="horizontal")
        mid.pack(fill="both", expand=True)
        self.text = tk.Text(mid, wrap="word", height=14, font=("TkDefaultFont", 12))
        mid.add(self.text, weight=3)
        self.text.bind("<Button-1>", self.on_text_click)
        self.img_label = ttk.Label(mid, text="(page scan)")
        mid.add(self.img_label, weight=2)

        tr = ttk.Frame(center)
        tr.pack(fill="x", pady=6)
        ttk.Button(tr, text="⏮", width=4, command=self.prev_sent).pack(side="left")
        ttk.Button(tr, text="‹", width=4, command=lambda: self.jump(self.cursor - 1)).pack(side="left")
        self.play_btn = ttk.Button(tr, text="▶ Play", command=self.toggle, width=10)
        self.play_btn.pack(side="left", padx=4)
        ttk.Button(tr, text="✋ +2.5s", command=self.hold).pack(side="left")
        ttk.Button(tr, text="🔁", width=4, command=self.repeat_word).pack(side="left", padx=2)
        self.loop_btn = ttk.Button(tr, text="🔂", width=4, command=self.toggle_loop)
        self.loop_btn.pack(side="left")
        ttk.Button(tr, text="›", width=4, command=lambda: self.jump(self.cursor + 1)).pack(side="left", padx=2)
        ttk.Button(tr, text="⏭", width=4, command=self.next_sent).pack(side="left")
        self.seek = ttk.Scale(tr, from_=0, to=0, orient="horizontal", command=lambda v: None)
        self.seek.pack(side="left", fill="x", expand=True, padx=8)
        self.seek.bind("<ButtonRelease-1>", lambda e: self.jump(int(float(self.seek.get()))))

        # right: studio
        right = ttk.Frame(body, padding=6)
        body.add(right, weight=1)
        ttk.Label(right, text="⏳ PAUSE ENGINE — my pace").pack(anchor="w")
        self.pace = self.slider(right, "Pace modifier (fine-tune)", 0.3, 2.5, self.engine.pace, 0.05, lambda v: setattr(self.engine, "pace", v))
        self.spl = self.slider(right, "Per-letter write time (s)", 0.15, 1.2, self.engine.spl, 0.01, lambda v: setattr(self.engine, "spl", v))
        self.think = self.slider(right, "Think overhead (s)", 0.0, 1.5, self.engine.think, 0.05, lambda v: setattr(self.engine, "think", v))
        self.sent = self.slider(right, "Sentence-end bonus (s)", 0.0, 3.0, self.engine.sent, 0.1, lambda v: setattr(self.engine, "sent", v))
        pr = ttk.Frame(right)
        pr.pack(fill="x", pady=4)
        for name in ("beginner", "classroom", "exam"):
            ttk.Button(pr, text=name, width=9, command=lambda n=name: self.preset(n)).pack(side="left", padx=2)
        ttk.Button(right, text="− Slower", command=lambda: self.nudge(0.1)).pack(fill="x", pady=1)
        ttk.Button(right, text="+ Faster", command=lambda: self.nudge(-0.1)).pack(fill="x", pady=1)
        ttk.Button(right, text="📏 Calibrate to my hand…", command=self.calibrate).pack(fill="x", pady=6)
        self.calib_var = tk.StringVar(value="Defaults keep your 0.60s/letter base.")
        ttk.Label(right, textvariable=self.calib_var, wraplength=240, foreground="gray", font=("TkDefaultFont", 9)).pack()

        self.spell_var = tk.BooleanVar(value=self.spell)
        self.double_var = tk.BooleanVar(value=self.double_say)
        self.blind_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="🔤 Spell hard words (8+)", variable=self.spell_var, command=self.sync_opts).pack(anchor="w")
        ttk.Checkbutton(right, text="🗣 Re-say long words (9+)", variable=self.double_var, command=self.sync_opts).pack(anchor="w")
        ttk.Checkbutton(right, text="🙈 Blind dictation", variable=self.blind_var, command=self.sync_opts).pack(anchor="w")
        ttk.Label(right, text="Speech rate / repeats").pack(anchor="w", pady=(8, 0))
        rr = ttk.Frame(right)
        rr.pack(fill="x")
        self.rate_var = tk.DoubleVar(value=self.rate)
        ttk.Spinbox(rr, from_=0.4, to=1.4, increment=0.05, textvariable=self.rate_var, width=6, command=self.sync_opts).pack(side="left")
        self.rep_var = tk.IntVar(value=self.repeats)
        ttk.Spinbox(rr, from_=1, to=4, increment=1, textvariable=self.rep_var, width=6, command=self.sync_opts).pack(side="left", padx=6)

        # M3 button roles: filled primary action, tonal everything else
        for w in self._all_widgets(self.root):
            if isinstance(w, ttk.Button):
                w.configure(style="Filled.TButton" if w is self.play_btn else "Tonal.TButton")
        # M3 surfaces for text widgets
        self.text.configure(bg=M3["container"], fg=M3["on_surface"], insertbackground=M3["primary"],
                            selectbackground=M3["primary"], selectforeground=M3["on_primary"],
                            highlightthickness=0, borderwidth=0, relief="flat")
        self.page_list.configure(bg=M3["container_low"], fg=M3["on_surface"],
                                 selectbackground=M3["secondary_container"],
                                 selectforeground=M3["on_secondary_container"],
                                 highlightthickness=0, borderwidth=0, relief="flat")

        self.root.bind("<Left>", lambda e: self.prev_sent())
        self.root.bind("<Right>", lambda e: self.next_sent())
        self.root.bind("r", lambda e: self.repeat_word())
        self.root.bind("h", lambda e: self.hold())
        self.root.bind("b", lambda e: (self.blind_var.set(not self.blind_var.get()), self.sync_opts()))
        self.fix_space_key()

    def slider(self, parent, label, lo, hi, val, step, cb):
        ttk.Label(parent, text=label).pack(anchor="w", pady=(6, 0))
        var = tk.DoubleVar(value=val)
        lab = ttk.Label(parent, text=f"{val:.2f}")
        lab.pack(anchor="e")

        def on_change(*a):
            v = round(float(var.get()) / step) * step
            lab.config(text=f"{v:.2f}")
            cb(v)
            self.refresh()
        var.trace_add("write", on_change)
        ttk.Scale(parent, from_=lo, to=hi, variable=var).pack(fill="x")
        return var

    def sync_opts(self):
        self.spell = self.spell_var.get()
        self.double_say = self.double_var.get()
        self.blind = self.blind_var.get()
        self.rate = float(self.rate_var.get())
        self.repeats = int(self.rep_var.get())
        self.refresh()

    def preset(self, name):
        p = PRESETS[name]
        self.engine.spl, self.engine.think, self.engine.pace, self.engine.sent = p["spl"], p["think"], p["pace"], p["sent"]
        self.pace.set(p["pace"]); self.spl.set(p["spl"]); self.think.set(p["think"]); self.sent.set(p["sent"])
        self.refresh()

    def nudge(self, d):
        self.engine.pace = min(2.5, max(0.3, round(self.engine.pace + d, 2)))
        self.pace.set(self.engine.pace)
        self.refresh()

    # ----- views -----
    def build_pages(self):
        self.page_list.delete(0, "end")
        for i, p in enumerate(self.doc.pages):
            head = " ".join(p["words"][:7])
            self.page_list.insert("end", f"P{i+1} — {head}… ({len(p['words'])}w)")
        self.render_text()

    def on_page_select(self, e):
        sel = self.page_list.curselection()
        if sel:
            pi = sel[0]
            g = next((i for i, w in enumerate(self.doc.flat) if w["page"] == pi), 0)
            self.jump(g)

    def render_text(self):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        if not self.doc.flat:
            return
        cur_page = self.doc.flat[self.cursor]["page"]
        start = next(i for i, w in enumerate(self.doc.flat) if w["page"] == cur_page)
        for k, w in enumerate(self.doc.pages[cur_page]["words"]):
            g = start + k
            if self.blind and g > self.cursor:
                show = "▮" * max(1, min(6, len(w)))
            else:
                show = w
            self.text.insert("end", show + " ", (f"g{g}",))
        self.text.config(state="disabled")

    def on_text_click(self, e):
        try:
            tags = self.text.tag_names(f"@{e.x},{e.y}")
            for t in tags:
                if t.startswith("g"):
                    self.jump(int(t[1:]))
                    break
        except Exception:
            pass
        return "break"

    def find(self):
        q = self.search_var.get().lower()
        if not q or not self.doc.flat:
            return
        for k in range(self.cursor + 1, len(self.doc.flat)):
            if q in self.doc.flat[k]["display"].lower():
                self.jump(k); return
        for k in range(0, self.cursor + 1):
            if q in self.doc.flat[k]["display"].lower():
                self.jump(k); return
        messagebox.showinfo("Find", "No match.")

    def prev_sent(self):
        if self.doc.flat:
            s, _ = self.sentence_bounds(max(0, self.cursor - 1))
            self.jump(s if self.cursor <= 0 else s)

    def next_sent(self):
        if self.doc.flat:
            _, e = self.sentence_bounds(self.cursor)
            self.jump(min(len(self.doc.flat) - 1, e + 1))

    def repeat_word(self):
        if not self.doc.flat:
            return
        was = self.playing
        if was:
            self.pause()
        self.jump(self.cursor)
        if not was:
            spoken = spoken_form(self.doc.flat[self.cursor]["display"]) or "blank"
            threading.Thread(target=lambda: self.play_wav(self.synth(spoken), -1), daemon=True).start()
        else:
            self.play()

    def toggle_loop(self):
        self.looping = not self.looping
        self.loop_btn.config(text="🔂●" if self.looping else "🔂")

    def ui(self, fn):
        """Schedule fn to run in the Tk main thread (thread-safe)."""
        self.ui_q.put(fn)

    def pump(self):
        try:
            while True:
                self.ui_q.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def set_status(self, s):
        self.ui(lambda s=s: self.status_var.set(s))

    def tick_progress(self):
        if self.playing:
            left = max(0.0, self.gap_end - time.time())
            pct = 100 * (1 - left / self.gap_total) if self.gap_total else 0
            self.prog["value"] = max(0, min(100, pct))
        self.root.after(100, self.tick_progress if self.playing else lambda: None)
        if not self.playing:
            self.prog["value"] = 0

    def refresh(self):
        if not self.doc.flat:
            return
        self.cursor = max(0, min(len(self.doc.flat) - 1, self.cursor))
        cur = self.doc.flat[self.cursor]
        first, last = self.opts(self.cursor)
        b = self.engine.breakdown(cur["display"], first, last)
        self.big_var.set(cur["display"])
        s, e = self.sentence_bounds(self.cursor)
        ctx = " ".join(w["display"] for w in self.doc.flat[s:e + 1])
        self.ctx_var.set(f"…{ctx}…   (word {self.cursor+1}/{len(self.doc.flat)}, page {cur['page']+1}/{len(self.doc.pages)})")
        self.bd_var.set(f"{b['total']:.2f}s = think {b['think']:.2f} + write {b['write']:.2f} "
                        f"({b['L']}×{self.engine.spl:.2f}×{b['curve']:.2f}) + syl {b['syl_extra']:.2f} "
                        f"+ extra {b['extra']:.2f} + punct {b['punct']:.2f} ×{self.engine.pace:.2f} {b['tags']}")
        try:
            self.seek.config(to=max(0, len(self.doc.flat) - 1))
            self.seek.set(self.cursor)
        except Exception:
            pass
        # text highlight
        self.text.config(state="normal")
        self.text.tag_remove("cur", "1.0", "end")
        self.text.tag_remove("done", "1.0", "end")
        self.text.tag_config("cur", background=M3["primary"], foreground=M3["on_primary"])
        self.text.tag_config("done", foreground=M3["variant"])
        cur_page = cur["page"]
        start = next(i for i, w in enumerate(self.doc.flat) if w["page"] == cur_page)
        # rebuild if page changed or blind on
        if getattr(self, "_shown_page", -1) != cur_page or self.blind:
            self._shown_page = cur_page
            self.render_text()
        for g in range(start, start + len(self.doc.pages[cur_page]["words"])):
            tag = f"g{g}"
            if g == self.cursor:
                self.text.tag_add("cur", f"{tag}.first", f"{tag}.last")
            elif g < self.cursor:
                self.text.tag_add("done", f"{tag}.first", f"{tag}.last")
        try:
            self.text.see("cur.first")
        except Exception:
            pass
        self.text.config(state="disabled")
        # page image
        if self.show_scan:
            img = self.doc.page_image(cur_page)
            if img is not None:
                kind, payload = img
                if kind == "pil":
                    self.photo = ImageTk.PhotoImage(payload)
                else:
                    self.photo = tk.PhotoImage(file=payload)
                self.img_label.config(image=self.photo, text="")
            else:
                self.img_label.config(image="", text="(no scan — text mode)")
        sel = self.page_list.curselection()
        if not sel or sel[0] != cur_page:
            self.page_list.selection_clear(0, "end")
            self.page_list.selection_set(cur_page)
            self.page_list.see(cur_page)

    # ----- calibration -----
    def calibrate(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Handwriting calibration")
        dlg.geometry("380x260")
        ttk.Label(dlg, text="5 words, easy → hard. After each, write it,\nthen press Done. I time you and fit your speed.",
                  justify="center").pack(pady=8)
        wvar = tk.StringVar(value="—")
        ttk.Label(dlg, textvariable=wvar, font=("TkDefaultFont", 32, "bold")).pack()
        svar = tk.StringVar(value="")
        ttk.Label(dlg, textvariable=svar, foreground="gray", font=("TkDefaultFont", 9)).pack()
        state = {"i": 0, "samples": [], "t0": 0.0}

        def step():
            if state["i"] >= len(CALIB_WORDS):
                xs = [L for L, _ in state["samples"]]
                ys = [t for _, t in state["samples"]]
                n = len(xs)
                mx, my = sum(xs) / n, sum(ys) / n
                den = sum((x - mx) ** 2 for x in xs)
                spl = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.55
                think = my - spl * mx
                spl = min(1.2, max(0.15, spl))
                think = min(1.5, max(0.0, think))
                self.engine.spl, self.engine.think, self.engine.pace = round(spl, 2), round(think, 2), 1.0
                self.spl.set(self.engine.spl); self.think.set(self.engine.think); self.pace.set(1.0)
                self.calib_var.set(f"Calibrated: {spl:.2f}s/letter + {think:.2f}s think — from YOUR hand.")
                wvar.set("✅"); svar.set(f"Done: {spl:.2f}s/letter, think {think:.2f}s. Applied!")
                start_btn.config(state="normal"); done_btn.config(state="disabled")
                self.refresh()
                return
            w = CALIB_WORDS[state["i"]]
            wvar.set(w)
            done_btn.config(state="disabled")
            threading.Thread(target=lambda: (self.play_wav(self.synth(w), -999), state.update(t0=time.time()),
                                             self.ui(lambda: done_btn.config(state="normal"))), daemon=True).start()

        def done():
            dt = time.time() - state["t0"]
            state["samples"].append((len(CALIB_WORDS[state["i"]]), dt))
            svar.set(svar.get() + f"“{CALIB_WORDS[state['i']]}” {dt:.1f}s. ")
            state["i"] += 1
            step()

        start_btn = ttk.Button(dlg, text="Start", command=lambda: (start_btn.config(state="disabled"), step()))
        start_btn.pack(pady=4)
        done_btn = ttk.Button(dlg, text="Done — next", command=done, state="disabled")
        done_btn.pack()


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    model = os.path.join(here, "en_US-lessac-medium.onnx")
    start = sys.argv[1] if len(sys.argv) > 1 and os.path.exists(sys.argv[1]) else None
    cls = TkinterDnD.Tk if HAVE_DND else tk.Tk
    root = cls()
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    App(root, model, start)
    root.mainloop()


if __name__ == "__main__":
    main()
