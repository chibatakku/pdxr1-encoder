"""
pdxr1_encoder.py（steps モード）で記録した CSV を可視化するスクリプト

出力するグラフ（文字はすべて英数字・論文形式）:
  1. 時間 vs 角度変位 [deg]
  2. 時間 vs 目標値との誤差 [arcsec]
  3. サイクル回数 vs 角度誤差の平均 [arcsec]（0°・10° 別、エラーバー = サイクル内の標準偏差）

使い方（PowerShell）:
  uv add pandas matplotlib          # 初回のみ
  uv run plot_pdxr1.py                              # 最新の pdxr1_steps_*.csv を使用
  uv run plot_pdxr1.py pdxr1_steps_20261106_200333.csv
"""

import glob
import os
import sys

import matplotlib.pyplot as plt
import pandas as pd

# ==========================================
# 論文形式のスタイル設定（グラフ内の文字はすべて英数字）
# ==========================================
plt.rcParams["font.family"] = "serif"
plt.rcParams["font.serif"] = ["Times New Roman"] + plt.rcParams["font.serif"]
plt.rcParams["mathtext.fontset"] = "cm"
plt.rcParams["font.size"] = 11
plt.rcParams["axes.linewidth"] = 1.0
plt.rcParams["lines.linewidth"] = 1.5
plt.rcParams["xtick.direction"] = "in"
plt.rcParams["ytick.direction"] = "in"
plt.rcParams["xtick.top"] = True
plt.rcParams["ytick.right"] = True
plt.rcParams["legend.frameon"] = False

COLORS = {0.0: "tab:blue", 10.0: "tab:orange"}
FIG_DIR = "fig"             # グラフの保存先フォルダ


def load(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["wallclock"])
    if "reached" in df.columns:
        n_bad = (df["reached"] == 0).sum()
        if n_bad:
            print(f"注意: 目標に到達しなかった {n_bad} 行を除外しました")
        df = df[df["reached"] == 1].copy()

    df["t_s"] = (df["wallclock"] - df["wallclock"].iloc[0]).dt.total_seconds()
    df["error_arcsec"] = df["angle_rel_arcsec"] - df["target_deg"] * 3600.0

    # 目標角ごとに何回目のサイクルかを振る（index の出現順に 1, 2, 3, ...）
    df["cycle"] = df.groupby("target_deg")["index"].rank(method="dense").astype(int)
    return df


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    """各サイクルの平均誤差・標準偏差・最大−最小"""
    s = (df.groupby(["target_deg", "cycle"])["error_arcsec"]
           .agg(mean="mean", std="std", pv=lambda x: x.max() - x.min(), n="count")
           .reset_index())
    return s


def print_summary(s: pd.DataFrame, df: pd.DataFrame) -> None:
    for tgt, g in s.groupby("target_deg"):
        print(f"\n=== 目標 {tgt:g}° ===")
        print(" cycle   平均[arcsec]   標準偏差[arcsec]   最大−最小[arcsec]")
        for _, r in g.iterrows():
            print(f"  {int(r.cycle):>3}   {r['mean']:>10.2f}   {r['std']:>14.2f}   {r.pv:>15.2f}")
        allv = df.loc[df["target_deg"] == tgt, "error_arcsec"]
        print(f"  サイクル平均の平均     : {g['mean'].mean():.2f} arcsec")
        print(f"  サイクル平均の標準偏差 : {g['mean'].std():.2f} arcsec（繰り返し位置決めのばらつき）")
        print(f"  サイクル内標準偏差の平均: {g['std'].mean():.2f} arcsec（静止中のばらつき）")
        print(f"  全サンプルの標準偏差   : {allv.std():.2f} arcsec")


def plot_angle(df, ax):
    for tgt, g in df.groupby("target_deg"):
        ax.plot(g["t_s"], g["angle_rel_deg"], ".", ms=3, color=COLORS.get(tgt), label=f"Target {tgt:g}$^\\circ$")
    ax.set_xlabel("Time [s]")
    ax.set_ylabel("Angular displacement [deg]")
    ax.legend()


def plot_error_time(df, ax):
    for tgt, g in df.groupby("target_deg"):
        ax.plot(g["t_s"], g["error_arcsec"], ".", ms=3, color=COLORS.get(tgt), label=f"Target {tgt:g}$^\\circ$")
    ax.axhline(0, color="k", lw=0.8)
    ax.set_xlabel("Time [s]")
    ax.set_ylabel("Error from target [arcsec]")
    ax.legend()


def plot_cycle_mean(s, ax):
    offset = {0.0: -0.08, 10.0: 0.08}       # 重ならないよう左右に少しずらす
    for tgt, g in s.groupby("target_deg"):
        sd_of_means = g["mean"].std()
        ax.errorbar(g["cycle"] + offset.get(tgt, 0), g["mean"], yerr=g["std"],
                    fmt="o-", capsize=4, color=COLORS.get(tgt),
                    label=(f"Target {tgt:g}$^\\circ$ (mean = {g['mean'].mean():.2f} arcsec, "
                           f"$\\sigma$ = {sd_of_means:.2f} arcsec)"))
    ax.axhline(0, color="k", lw=0.8)
    ax.set_xlabel("Cycle number")
    ax.set_ylabel("Mean angular error [arcsec]")
    ax.set_xticks(sorted(s["cycle"].unique()))
    ax.legend(loc="best")


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        files = sorted(f for f in glob.glob("pdxr1_steps_*.csv")
                       if not f.endswith("_summary.csv"))   # 集計ファイルは除外
        if not files:
            sys.exit("pdxr1_steps_*.csv が見つかりません。CSV のパスを引数で指定してください。")
        path = files[-1]
    print("読み込み:", path)

    df = load(path)
    s = summarize(df)
    print_summary(s, df)

    stem = os.path.splitext(path)[0]
    s.to_csv(f"{stem}_summary.csv", index=False, encoding="utf-8-sig")

    # グラフは fig フォルダに保存（なければ作成）
    os.makedirs(FIG_DIR, exist_ok=True)
    base = os.path.basename(stem)
    saved = []
    plots = [("angle", plot_angle, df), ("error", plot_error_time, df), ("cycle", plot_cycle_mean, s)]
    for name, func, data in plots:
        fig, ax = plt.subplots(figsize=(7, 3.5))
        func(data, ax)
        fig.tight_layout()
        out = os.path.join(FIG_DIR, f"{base}_{name}.png")
        fig.savefig(out, dpi=200)
        saved.append(out)

    print("\n保存:")
    for f in saved + [f"{stem}_summary.csv"]:
        print("  ", f)
    plt.show()


if __name__ == "__main__":
    main()