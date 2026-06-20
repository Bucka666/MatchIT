"""
profile_tool.py - SKU Profile Entry Tool (Vertical-aware)

Reads field definitions from verticals/<n>/vertical.json.
Displays FRONT and BACK images side by side for each SKU.
Dynamically generates UI fields from the vertical config.

Saves to sku_profiles.json in project root.
Can stop and resume at any time — already-entered SKUs are pre-populated.

Usage:
    python profile_tool.py
"""

import tkinter as tk
from tkinter import messagebox
import json
import sqlite3
import os
from pathlib import Path
from PIL import Image, ImageTk
import app
from vertical_loader import load_vertical, get_vertical, get_field_defs, get_categories

PROFILES_PATH = os.path.join(app.app.root_path, "sku_profiles.json")
MAX_DISPLAY_H = 480
MAX_DISPLAY_W = 280

VERTICAL = load_vertical(str(app.CFG.get("vertical", "keys")), app.app.root_path)


def load_profiles():
    if os.path.exists(PROFILES_PATH):
        try: return json.loads(Path(PROFILES_PATH).read_text(encoding="utf-8"))
        except Exception: return {}
    return {}

def save_profiles(profiles):
    Path(PROFILES_PATH).write_text(json.dumps(profiles, indent=2), encoding="utf-8")

def get_all_skus():
    db_path = app.get_images_db_path()
    conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
    try: rows = conn.execute("SELECT sku, original_filename, path FROM images ORDER BY sku").fetchall()
    finally: conn.close()
    sku_images = {}
    for row in rows:
        sku = (row["sku"] or "").strip(); orig = (row["original_filename"] or "").upper(); path = row["path"]
        if not sku or not path or not os.path.exists(path): continue
        if sku not in sku_images: sku_images[sku] = {"FRONT": None, "BACK": None}
        if "_FRONT" in orig: sku_images[sku]["FRONT"] = path
        elif "_BACK" in orig: sku_images[sku]["BACK"] = path
    return [(sku, imgs["FRONT"], imgs["BACK"]) for sku, imgs in sorted(sku_images.items()) if imgs["FRONT"]]

def _load_display_image(path, max_w, max_h):
    with Image.open(path) as im:
        im = im.convert("RGB"); w, h = im.size
        scale = min(max_w / float(w), max_h / float(h), 1.0)
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR)
        return ImageTk.PhotoImage(im)


class ProfileTool:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{get_vertical().get('name', 'MatchIT')} — SKU Profile Tool")
        self.profiles = load_profiles()
        self.skus = get_all_skus()
        self.total_all = len(self.skus)
        self.idx = 0
        self.field_defs = get_field_defs()
        self.categories = get_categories()
        if not self.skus:
            messagebox.showinfo("No SKUs", "No SKUs found."); root.destroy(); return
        self._build_ui(); self._load_sku()

    def _build_ui(self):
        BG="#1e1e2e"; FG="#cdd6f4"; BTN_BG="#313244"; BTN_ACT="#45475a"
        SEL_BG="#89b4fa"; SEL_FG="#1e1e2e"; HDR_FG="#cba6f7"; WARN_FG="#f38ba8"
        self.BG=BG; self.FG=FG; self.BTN_BG=BTN_BG; self.BTN_ACT=BTN_ACT
        self.SEL_BG=SEL_BG; self.SEL_FG=SEL_FG
        self.root.configure(bg=BG); self.root.resizable(True, True); self.root.minsize(700, 550)
        btn_cfg = dict(font=("Segoe UI", 11, "bold"), relief="flat", cursor="hand2", padx=16, pady=6)

        bf = tk.Frame(self.root, bg=BG); bf.pack(side="bottom", fill="x", padx=12, pady=(4,12))
        tk.Button(bf, text="<< Start", bg=BTN_BG, fg=FG, activebackground=BTN_ACT, command=self._go_to_start, **btn_cfg).pack(side="left", padx=4)
        tk.Button(bf, text="< Prev", bg=BTN_BG, fg=FG, activebackground=BTN_ACT, command=self._prev, **btn_cfg).pack(side="left", padx=4)
        tk.Button(bf, text="SKIP >", bg=BTN_BG, fg=FG, activebackground=BTN_ACT, command=self._skip, **btn_cfg).pack(side="left", padx=4)
        tk.Button(bf, text="SAVE & NEXT >", bg="#40a02b", fg="#ffffff", activebackground="#2d7a1f", command=self._save_and_next, **btn_cfg).pack(side="right", padx=4)
        self.lbl_status = tk.Label(bf, text="", font=("Segoe UI",10), bg=BG, fg=WARN_FG); self.lbl_status.pack(side="right", padx=12)
        tk.Frame(self.root, bg="#45475a", height=1).pack(side="bottom", fill="x")

        cf = tk.Frame(self.root, bg=BG); cf.pack(side="top", fill="both", expand=True)
        self._canvas = tk.Canvas(cf, bg=BG, highlightthickness=0)
        sb = tk.Scrollbar(cf, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self._canvas.pack(side="left", fill="both", expand=True)
        self._si = tk.Frame(self._canvas, bg=BG)
        self._cw = self._canvas.create_window((0,0), window=self._si, anchor="nw")
        self._si.bind("<Configure>", lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(self._cw, width=e.width))
        self._canvas.bind_all("<MouseWheel>", lambda e: self._canvas.yview_scroll(int(-1*(e.delta/120)), "units"))
        C = self._si

        hdr = tk.Frame(C, bg=BG); hdr.pack(fill="x", padx=12, pady=(10,4))
        self.lbl_sku = tk.Label(hdr, text="", font=("Segoe UI",18,"bold"), bg=BG, fg=HDR_FG); self.lbl_sku.pack(side="left")
        self.lbl_progress = tk.Label(hdr, text="", font=("Segoe UI",11), bg=BG, fg=FG); self.lbl_progress.pack(side="right")

        img_frame = tk.Frame(C, bg=BG); img_frame.pack(padx=12, pady=4)
        v = get_vertical(); labels = v.get("image_labels", {"front":"FRONT","back":"BACK"})
        for col, key in enumerate(["front","back"]):
            f = tk.Frame(img_frame, bg=BG); f.grid(row=0, column=col, padx=8)
            tk.Label(f, text=labels.get(key,key).upper(), font=("Segoe UI",10,"bold"), bg=BG, fg=FG).pack()
            lbl = tk.Label(f, bg="#2a2a3e", relief="flat", width=MAX_DISPLAY_W, height=MAX_DISPLAY_H); lbl.pack()
            if col==0: self.img_lbl_front = lbl
            else: self.img_lbl_back = lbl
        self._tk_front = None; self._tk_back = None

        # Category selector
        cat_frame = tk.LabelFrame(C, text="Category", font=("Segoe UI",10,"bold"), bg=BG, fg=HDR_FG, bd=1, relief="groove", padx=8, pady=6)
        cat_frame.pack(fill="x", padx=20, pady=(6,6))
        self._cat_var = tk.StringVar(value=""); self._cat_btns = []
        cats = list(self.categories.items())
        r1 = tk.Frame(cat_frame, bg=BG); r1.pack(fill="x", pady=(0,4))
        r2 = tk.Frame(cat_frame, bg=BG); r2.pack(fill="x")
        b = tk.Button(r1, text="?", font=("Segoe UI",10,"bold"), relief="flat", cursor="hand2", padx=10, pady=4, bg=BTN_BG, fg=FG, activebackground=BTN_ACT, command=lambda: self._on_cat_selected(""))
        b.pack(side="left", padx=3); self._cat_btns.append(("", b))
        for i, (cid, cdef) in enumerate(cats):
            parent = r1 if i < 4 else r2
            b = tk.Button(parent, text=cdef.get("label",cid), font=("Segoe UI",10,"bold"), relief="flat", cursor="hand2", padx=10, pady=4, bg=BTN_BG, fg=FG, activebackground=BTN_ACT, command=lambda v=cid: self._on_cat_selected(v))
            b.pack(side="left", padx=3); self._cat_btns.append((cid, b))

        # Dynamic profile fields from vertical config
        self._field_widgets = {}
        for fdef in self.field_defs:
            fid = fdef["id"]; ftype = fdef.get("type","select")
            frame = tk.LabelFrame(C, text=fdef.get("label",fid), font=("Segoe UI",10,"bold"), bg=BG, fg=HDR_FG, bd=1, relief="groove", padx=8, pady=6)
            frame.pack(fill="x", padx=20, pady=(0,6))
            container = tk.Frame(frame, bg=BG); container.pack(fill="x")
            wi = {"frame": frame, "container": container, "type": ftype, "def": fdef, "btns": []}

            if ftype == "select":
                var = tk.StringVar(value="")
                for ov, ol in fdef.get("options", []):
                    b = tk.Button(container, text=ol, font=("Segoe UI",10,"bold"), relief="flat", cursor="hand2", padx=10, pady=4, bg=BTN_BG, fg=FG, activebackground=BTN_ACT, command=lambda fid=fid, v=ov: self._on_field_selected(fid, v))
                    b.pack(side="left", padx=3); wi["btns"].append((ov, b))
                wi["var"] = var
            elif ftype == "int_range":
                var = tk.IntVar(value=-1)
                b = tk.Button(container, text="?", font=("Segoe UI",10,"bold"), relief="flat", cursor="hand2", padx=10, pady=4, bg=BTN_BG, fg=FG, activebackground=BTN_ACT, command=lambda fid=fid: self._on_field_selected(fid, -1))
                b.pack(side="left", padx=3); wi["btns"].append((-1, b))
                for n in range(fdef.get("min",0), fdef.get("max",10)+1):
                    b = tk.Button(container, text=str(n), font=("Segoe UI",10,"bold"), relief="flat", cursor="hand2", padx=10, pady=4, bg=BTN_BG, fg=FG, activebackground=BTN_ACT, command=lambda fid=fid, v=n: self._on_field_selected(fid, v))
                    b.pack(side="left", padx=3); wi["btns"].append((n, b))
                wi["var"] = var
            elif ftype == "float_select":
                wi["var"] = tk.DoubleVar(value=-1.0)

            self._field_widgets[fid] = wi

    def _on_cat_selected(self, val):
        self._cat_var.set(val)
        for v, b in self._cat_btns:
            b.config(bg=self.SEL_BG if v==val else self.BTN_BG, fg=self.SEL_FG if v==val else self.FG)
        self._update_field_visibility(val)

    def _on_field_selected(self, fid, val):
        w = self._field_widgets.get(fid)
        if not w: return
        w["var"].set(val)
        for bval, b in w["btns"]:
            match = abs(float(bval)-float(val)) < 0.01 if w["type"]=="float_select" else bval==val
            b.config(bg=self.SEL_BG if match else self.BTN_BG, fg=self.SEL_FG if match else self.FG)

    def _rebuild_float_buttons(self, fid, values):
        w = self._field_widgets.get(fid)
        if not w: return
        for child in w["container"].winfo_children(): child.destroy()
        w["btns"] = []
        b = tk.Button(w["container"], text="?", font=("Segoe UI",10,"bold"), relief="flat", cursor="hand2", padx=10, pady=4, bg=self.BTN_BG, fg=self.FG, activebackground=self.BTN_ACT, command=lambda: self._on_field_selected(fid, -1.0))
        b.pack(side="left", padx=3); w["btns"].append((-1.0, b))
        for n in values:
            lbl = str(int(n)) if n == int(n) else str(n)
            b = tk.Button(w["container"], text=lbl, font=("Segoe UI",10,"bold"), relief="flat", cursor="hand2", padx=10, pady=4, bg=self.BTN_BG, fg=self.FG, activebackground=self.BTN_ACT, command=lambda v=n: self._on_field_selected(fid, v))
            b.pack(side="left", padx=3); w["btns"].append((n, b))

    def _update_field_visibility(self, category):
        cat_def = self.categories.get(category, {})
        show_ids = set(cat_def.get("show", []))
        for fid, w in self._field_widgets.items():
            if fid in show_ids:
                w["frame"].pack(fill="x", padx=20, pady=(0,6))
                if w["type"] == "float_select":
                    vbc = w["def"].get("values_by_category", {})
                    self._rebuild_float_buttons(fid, vbc.get(category, []))
            else:
                w["frame"].pack_forget()
                w["var"].set(w["def"].get("default", ""))

    def _load_sku(self):
        if self.idx >= len(self.skus): self._finished(); return
        sku, fp, bp = self.skus[self.idx]
        saved = self.profiles.get(sku)
        self.lbl_sku.config(text=f"SKU: {sku}{' ✓' if saved else ''}")
        self.lbl_progress.config(text=f"{self.idx+1} / {self.total_all}   {len(self.profiles)} profiled")
        self.lbl_status.config(text="")
        try:
            self._tk_front = _load_display_image(fp, MAX_DISPLAY_W, MAX_DISPLAY_H)
            self.img_lbl_front.config(image=self._tk_front)
        except Exception: self.img_lbl_front.config(image="", text="No image")
        if bp and os.path.exists(bp):
            try:
                self._tk_back = _load_display_image(bp, MAX_DISPLAY_W, MAX_DISPLAY_H)
                self.img_lbl_back.config(image=self._tk_back)
            except Exception: self.img_lbl_back.config(image="", text="No image")
        else: self.img_lbl_back.config(image="", text="No back image"); self._tk_back = None
        if saved:
            self._on_cat_selected(saved.get("key_type","") or saved.get("category",""))
            for fid, w in self._field_widgets.items():
                self._on_field_selected(fid, saved.get(fid, w["def"].get("default","")))
        else:
            self._on_cat_selected("")
            for fid, w in self._field_widgets.items():
                self._on_field_selected(fid, w["def"].get("default",""))

    def _save_and_next(self):
        result = {"key_type": self._cat_var.get()}
        for fid, w in self._field_widgets.items(): result[fid] = w["var"].get()
        self.profiles[self.skus[self.idx][0]] = result
        save_profiles(self.profiles); self.idx += 1; self._load_sku()

    def _skip(self): self.idx += 1; self._load_sku()
    def _prev(self):
        if self.idx > 0: self.idx -= 1
        self._load_sku()
    def _go_to_start(self): self.idx = 0; self._load_sku()
    def _finished(self):
        messagebox.showinfo("End of list", f"Reached end of all {self.total_all} SKUs.\n{len(self.profiles)} have profiles saved.\n\nUse << Start to go back.")

if __name__ == "__main__":
    root = tk.Tk(); ProfileTool(root); root.mainloop()