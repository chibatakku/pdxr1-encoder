"""
pdxr1_release_stability.py で記録した CSV を可視化するスクリプト

出力するグラフ（文字はすべて英数字・論文形式）:
  1. 解除からの時間 vs 目標値との誤差 [arcsec]（閉ループ保持区間 + 解除後）
  2. 解除からの時間 vs 解除後の角度変化 [arcsec]（基準 = 解除後 T_REF_S 秒の値、線形フィット付き）
  3. （複数サイクルのとき）サイクル回数 vs 解除後の最終誤差・ドリフト量 [arcsec]

時間原点 t = 0 は PC 側で MoveStop を呼ぶ直前の時刻。

使い方（PowerShell）:
  uv add pandas matplotlib          # 初回のみ
  uv run plot_pdxr1_release.py                                   # 最新の pdxr1_release_*.csv
  uv run plot_pdxr1_release.py pdxr1_release_20261113_142957.csv
"""

import glob
import os
import sys

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ==========================================
# 論文形式のスタイル設定（グラフ内の文字はすべて英数字）
# ==========================================
plt.rcParams["font.family"] = "serif"
plt.rcParams["font.serif"] = ["Times New Roman"] + plt.rcParams["font.serif"]
plt.rcParams["mathtext.fontset"] = "cm"
plt.rcParams["font.size"] = 11
plt.rcParams["axes.linewidth"] = 1.0
plt.rcParams["lines.linewidth"] = 1.2
plt.rcParams["xtick.direction"] = "in"
plt.rcParams["ytick.direction"] = "in"
plt.rcParams["xtick.top"] = True
plt.rcParams["ytick.right"] = True
plt.rcParams["legend.frameon"] = False

COLORS = {0.0: "tab:blue", 10.0: "tab:orange"}
FIG_DIR = "fig"             # グラフの保存先フォルダ

T_REF_S = 0.2               # 解除後の基準時刻 [s]（解除直後の過渡・ポーリング遅れを避ける）
END_WINDOW_S = 1.0          # 最終値として平均する区間 [s]（記録終了前の 1 s）
ARCSEC_PER_COUNT = 0.09


def load(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["wallclock"])
    rel = df[df["phase"] == "release"].set_index("cycle")["wallclock"]
    df = df[df["phase"].isin(["pre", "post"])].copy()

    # pre 区間にも「解除からの時間」を付ける（wallclock の差から計算、pre は負）
    t_rel = (df["wallclock"] - df["cycle"].map(rel)).dt.total_seconds()
    df["t_s"] = df["t_from_release_s"].where(df["phase"] == "post", t_rel).astype(float)

    df["error_arcsec"] = df["dev_from_target_arcsec"].astype(float)
    df["counts"] = df["counts"].astype(int)

    # 解除後の角度変化（基準 = 解除後 T_REF_S 秒に最も近いサンプル）
    ref = {}
    for c, g in df[df["phase"] == "post"].groupby("cycle"):
        ref[c] = g.loc[(g["t_s"] - T_REF_S).abs().idxmin(), "error_arcsec"]
    df["change_arcsec"] = df["error_arcsec"] - df["cycle"].map(ref)
    return df


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    """各サイクルの評価値 [arcsec]"""
    rows = []
    for (c, tgt), g in df.groupby(["cycle", "target_deg"]):
        pre = g[g["phase"] == "pre"]["error_arcsec"]
        post = g[(g["phase"] == "post") & (g["t_s"] >= T_REF_S)]
        t_end = post["t_s"].max()
        last = post[post["t_s"] >= t_end - END_WINDOW_S]["error_arcsec"]
        e_ref = post["error_arcsec"].iloc[0]
        slope = np.polyfit(post["t_s"], post["error_arcsec"], 1)[0] if len(post) > 2 else np.nan
        rows.append({
            "cycle": c + 1,
            "target_deg": tgt,
            "pre_mean": pre.mean(),
            "pre_std": pre.std(),
            "pre_pv": pre.max() - pre.min(),
            "err_at_ref": e_ref,                       # 解除後 T_REF_S 秒の誤差
            "err_final": last.mean(),                  # 最終誤差（最後の END_WINDOW_S 秒の平均）
            "drift": last.mean() - e_ref,              # T_REF_S → 最終 の変化量
            "rate_per_s": slope,                       # 解除後の線形ドリフト速度 [arcsec/s]
            "post_std": post["error_arcsec"].std(),
            "post_pv": post["error_arcsec"].max() - post["error_arcsec"].min(),
            "hold_s": t_end,
        })
    return pd.DataFrame(rows)


def print_summary(s: pd.DataFrame) -> None:
    print("\n各サイクルの評価値 [arcsec]（誤差 = 実測角 − 目標角）")
    print(f"  pre   : 閉ループ保持中（解除前）の誤差  mean / std / p-v")
    print(f"  ref   : 解除後 {T_REF_S:g} s の誤差,  final: 最後の {END_WINDOW_S:g} s の平均誤差")
    print(f"  drift : final − ref,  rate: 解除後の線形ドリフト速度 [arcsec/s]")
    hdr = (f"{'cycle':>5}{'target':>8}{'pre mean':>10}{'pre std':>9}{'pre p-v':>9}"
           f"{'ref':>8}{'final':>8}{'drift':>8}{'rate':>9}{'hold[s]':>9}")
    print("-" * len(hdr))
    print(hdr)
    print("-" * len(hdr))
    for _, r in s.iterrows():
        print(f"{int(r.cycle):>5}{r.target_deg:>8g}{r.pre_mean:>+10.2f}{r.pre_std:>9.2f}{r.pre_pv:>9.2f}"
              f"{r.err_at_ref:>+8.2f}{r.err_final:>+8.2f}{r.drift:>+8.2f}{r.rate_per_s:>+9.4f}"
              f"{r.hold_s:>9.1f}")
    print("-" * len(hdr))

    if len(s) > 1:
        for t, g in s.groupby("target_deg"):
            print(f"\n[目標 {t:g}°]  {len(g)} サイクル")
            print(f"  最終誤差   平均 {g['err_final'].mean():+.2f} / 標準偏差 {g['err_final'].std():.2f} arcsec")
            print(f"  ドリフト量 平均 {g['drift'].mean():+.2f} / 標準偏差 {g['drift'].std():.2f} arcsec")


def _label(c, tgt, n_cycles):
    return f"Target {tgt:g}$^\\circ$" + (f" (#{c + 1})" if n_cycles > 1 else "")


def plot_error_time(df, ax):
    n = df["cycle"].nunique()
    for (c, tgt), g in df.groupby(["cycle", "target_deg"]):
        ax.plot(g["t_s"], g["error_arcsec"], "-", lw=0.9,
                color=COLORS.get(tgt) if n == 1 else None, label=_label(c, tgt, n))
    ax.axvspan(df["t_s"].min(), 0, color="0.9", zorder=0)
    ax.axvline(0, color="k", ls="--", lw=0.8)
    ax.axhline(0, color="k", lw=0.6)
    ymax = ax.get_ylim()[1]
    ax.text(df["t_s"].min() / 2, ymax, "Closed loop", ha="center", va="top", fontsize=9)
    ax.text(df["t_s"].max() / 2, ymax, "Released", ha="center", va="top", fontsize=9)
    ax.set_xlabel("Time from closed-loop release [s]")
    ax.set_ylabel("Error from target [arcsec]")
    ax.legend(loc="lower right")


def plot_drift(df, ax):
    n = df["cycle"].nunique()
    post = df[(df["phase"] == "post")]
    for (c, tgt), g in post.groupby(["cycle", "target_deg"]):
        color = COLORS.get(tgt) if n == 1 else None
        line, = ax.plot(g["t_s"], g["change_arcsec"], "-", lw=0.9, color=color,
                        label=_label(c, tgt, n))
        fit = g[g["t_s"] >= T_REF_S]
        if n == 1 and len(fit) > 2:
            k, b = np.polyfit(fit["t_s"], fit["change_arcsec"], 1)
            tt = np.array([T_REF_S, fit["t_s"].max()])
            ax.plot(tt, k * tt + b, "--", lw=1.0, color="k",
                    label=f"Linear fit ({k:+.3f} arcsec/s)")
    ax.axhline(0, color="k", lw=0.6)
    ax.axvline(T_REF_S, color="0.5", ls=":", lw=0.8)
    ax.set_xlim(left=0)
    ax.set_xlabel("Time from closed-loop release [s]")
    ax.set_ylabel(f"Change from $t$ = {T_REF_S:g} s [arcsec]")
    ax.legend(loc="best")


def plot_cycle(s, ax):
    for tgt, g in s.groupby("target_deg"):
        ax.plot(g["cycle"], g["err_final"], "o-", color=COLORS.get(tgt),
                label=f"Final error, target {tgt:g}$^\\circ$")
        ax.plot(g["cycle"], g["drift"], "s--", mfc="none", color=COLORS.get(tgt),
                label=f"Drift, target {tgt:g}$^\\circ$")
    ax.axhline(0, color="k", lw=0.6)
    ax.set_xlabel("Cycle number")
    ax.set_ylabel("Angle [arcsec]")
    ax.set_xticks(sorted(s["cycle"].unique()))
    ax.legend(loc="best", fontsize=9)


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        files = sorted(f for f in glob.glob("pdxr1_release_*.csv")
                       if not f.endswith(("_summary.csv", "_stats.csv")))
        if not files:
            sys.exit("pdxr1_release_*.csv が見つかりません。CSV のパスを引数で指定してください。")
        path = files[-1]
    print("読み込み:", path)

    df = load(path)
    s = summarize(df)
    print_summary(s)

    stem = os.path.splitext(path)[0]
    stats_path = f"{stem}_stats.csv"
    s.to_csv(stats_path, index=False, encoding="utf-8-sig")

    os.makedirs(FIG_DIR, exist_ok=True)
    base = os.path.basename(stem)
    plots = [("error", plot_error_time, df), ("drift", plot_drift, df)]
    if len(s) > 1:
        plots.append(("cycle", plot_cycle, s))

    saved = []
    for name, func, data in plots:
        fig, ax = plt.subplots(figsize=(7, 3.5))
        func(data, ax)
        fig.tight_layout()
        out = os.path.join(FIG_DIR, f"{base}_{name}.png")
        fig.savefig(out, dpi=200)
        saved.append(out)

    print("\n保存:")
    for f in saved + [stats_path]:
        print("  ", f)
    plt.show()


if __name__ == "__main__":
    main()