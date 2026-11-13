"""
PDXR1/M + PDXC2 閉ループ解除後の安定性評価スクリプト（Kinesis + pythonnet）

pdxr1_encoder.py をもとに作成。各サイクルで
  1. 閉ループで目標角へ位置決め → 整定待ち
  2. 閉ループ保持状態のエンコーダ値を PRE_S 秒記録（比較用）
  3. 閉ループ解除（Kinesis GUI の "Stop" 相当）
  4. 解除後のエンコーダ値を HOLD_S 秒記録
を繰り返し、生データ CSV とサイクルごとのサマリ CSV を出力する。

使い方（PowerShell、Kinesis GUI は閉じておく）:
  uv run pdxr1_release_stability.py

単位:
  回転ステージでは 360° = 14,400,000 エンコーダカウント
  → 1 カウント = 0.000025° (= 0.09 arcsec = 0.4363 µrad)
"""

import csv
import os
import statistics
import time
from datetime import datetime

# ===== 設定 ==============================================================
SERIAL = "112569560"        # PDXC2 のシリアル番号
KINESIS = r"C:\Program Files\Thorlabs\Kinesis"

os.add_dll_directory(KINESIS)
os.environ["PATH"] = KINESIS + ";" + os.environ["PATH"]

import clr  # noqa: E402

POLL_MS = 50                # 位置（ステータス）の更新間隔 [ms]
DO_OPTIMIZE = False         # True: Performance Optimize を実行（負荷を変えたとき）
SPEED_DEG_S = 20.0          # 閉ループ移動速度 [°/s]
ACCEL_DEG_S2 = 250.0        # 閉ループ加速度 [°/s²]
THETA0_DEG = 0.0            # 基準オフセット [°]

# --- 安定性評価 ---
START_DEG = 0.0             # 開始位置 [°]（ここへ移動してから測定開始。記録しない）
TARGETS_DEG = [10.0]        # 位置決めする目標角の列（各目標で 1 サイクル）
                            #   例: [10.0, 0.0] * 5 で 0↔10° を5往復
SETTLE_S = 1.0              # 到達後、記録開始までの整定待ち [s]
PRE_S = 2.0                 # 解除前（閉ループ保持中）の記録時間 [s]
HOLD_S = 10.0               # 解除後の記録時間 [s]
SAMPLE_MS = POLL_MS         # 記録間隔 [ms]（POLL_MS より短くしても値は更新されない）

# 閉ループ解除の方法
#   "stop"      : 停止コマンド（Kinesis GUI の Stop ボタン相当）← 推奨
#   "open_loop" : 開ループモードへ切替（※開ループ中は GetCurrentPosition が
#                 エンコーダ値でなくステップ数を返す可能性あり。要確認）
RELEASE_METHOD = "stop"
STOP_METHOD_CANDIDATES = ("MoveStop", "Stop", "StopImmediate")

# サマリ指標の区間
JUMP_WINDOW_S = 0.25        # 解除直後この時間の平均を「解除直後の値」とする [s]
END_WINDOW_S = 1.0          # 記録終盤この時間の平均を「最終値」とする [s]

PLOT = True                 # True: matplotlib があれば解除後ドリフトの図を保存
# ========================================================================

COUNTS_PER_REV = 14_400_000
DEG_PER_COUNT = 360.0 / COUNTS_PER_REV
ARCSEC_PER_DEG = 3600.0
ARCSEC_PER_COUNT = DEG_PER_COUNT * ARCSEC_PER_DEG   # = 0.09 arcsec

clr.AddReference(KINESIS + r"\Thorlabs.MotionControl.DeviceManagerCLI.dll")
clr.AddReference(KINESIS + r"\Thorlabs.MotionControl.GenericPiezoCLI.dll")
clr.AddReference(KINESIS + r"\Thorlabs.MotionControl.Benchtop.PiezoCLI.dll")

from Thorlabs.MotionControl.DeviceManagerCLI import DeviceManagerCLI, DeviceConfiguration  # noqa: E402  # type: ignore
from Thorlabs.MotionControl.Benchtop.PiezoCLI.PDXC2 import InertiaStageController  # noqa: E402  # type: ignore
from Thorlabs.MotionControl.GenericPiezoCLI.Piezo import PiezoControlModeTypes  # noqa: E402  # type: ignore


# ---------------------------------------------------------------- 単位変換
def deg_to_counts(deg: float) -> int:
    return int(round(deg / DEG_PER_COUNT))


def counts_to_deg(counts: int) -> float:
    return counts * DEG_PER_COUNT


def counts_to_rel_arcsec(counts: int) -> float:
    """基準オフセット THETA0_DEG からの相対角 [arcsec]"""
    return counts * ARCSEC_PER_COUNT - THETA0_DEG * ARCSEC_PER_DEG


def read_counts(dev) -> int:
    return int(dev.GetCurrentPosition())


def read_status_bits(dev):
    """ステータスビット（取得できなければ None）。閉ループ解除の確認用。"""
    try:
        return int(dev.Status.Status)
    except Exception:
        return None


def stamp():
    return datetime.now().isoformat(timespec="milliseconds")


# ---------------------------------------------------------------- 接続・初期化
def connect():
    DeviceManagerCLI.BuildDeviceList()
    found = list(DeviceManagerCLI.GetDeviceList(InertiaStageController.DevicePrefix_PDXC2))
    if SERIAL not in found:
        raise RuntimeError(f"PDXC2 {SERIAL} が見つかりません。検出: {found}")

    dev = InertiaStageController.CreateInertiaStageController(SERIAL)
    dev.Connect(SERIAL)
    if not dev.IsSettingsInitialized():
        dev.WaitForSettingsInitialized(10000)
    dev.StartPolling(POLL_MS)
    time.sleep(0.5)
    dev.EnableDevice()
    time.sleep(0.5)
    dev.GetPDXC2Configuration(SERIAL, DeviceConfiguration.DeviceSettingsUseOptionType.UseDeviceSettings)
    print("Connected:", dev.GetDeviceInfo().Description)
    return dev


def setup_closed_loop(dev):
    dev.SetPositionControlMode(PiezoControlModeTypes.CloseLoop)
    time.sleep(0.5)

    if DO_OPTIMIZE:
        print("Performance Optimize 実行中（1〜2分）...")
        dev.PulseParamsAcquireStart()
        time.sleep(0.5)
        while not dev.Status.IsPulseParamsAcquired:
            time.sleep(0.5)

    print("Homing...")
    dev.Home(60000)
    print(f"Homed. {counts_to_deg(read_counts(dev)):.6f} deg")

    p = dev.GetClosedLoopParameters()
    p.RefSpeed = deg_to_counts(SPEED_DEG_S)
    p.Acceleration = deg_to_counts(ACCEL_DEG_S2)
    dev.SetClosedLoopParameters(p)


# ---------------------------------------------------------------- 移動
START_TOL_COUNTS = 400      # 「動き出した」と判定する移動量 = 0.01°（閉ループの揺れ ±100 カウントより大きく）


def _command_move(dev, target):
    # 前の動作（ホーミング・保持など）を一度止めてから指令する。
    # ホーミング直後は MoveStart が無視されることがあり、MoveStop 後は動くことを確認済み。
    try:
        dev.MoveStop()
        time.sleep(0.2)
    except Exception:
        pass
    dev.SetClosedLoopTarget(target)
    time.sleep(0.1)
    dev.MoveStart()


def move_to_deg(dev, target_deg, tol_counts=40, timeout_s=30.0,
                retry_s=2.0, max_retries=3):
    """絶対角へ閉ループ移動し、目標近傍で値が落ち着くまで待つ"""
    target = deg_to_counts(target_deg + THETA0_DEG)
    start = read_counts(dev)
    if abs(start - target) <= tol_counts:
        return True
    start_tol = min(START_TOL_COUNTS, abs(target - start) // 2)

    _command_move(dev, target)
    t0 = t_cmd = time.time()
    stable, retries = 0, 0
    while time.time() - t0 < timeout_s:
        time.sleep(POLL_MS / 1000)
        c = read_counts(dev)
        if abs(c - target) <= tol_counts:
            stable += 1
            if stable >= 4:
                return True
            continue
        stable = 0
        not_started = abs(c - start) <= start_tol
        if not_started and time.time() - t_cmd > retry_s and retries < max_retries:
            retries += 1
            print(f"  移動が始まらないため指令を再送します（{retries}/{max_retries}）")
            _command_move(dev, target)
            t_cmd = time.time()
    print(f"警告: {target_deg}° に到達しませんでした（現在 {counts_to_deg(read_counts(dev)):.5f}°）")
    return False


# ---------------------------------------------------------------- 閉ループ解除・復帰
def release_closed_loop(dev) -> str:
    """閉ループを解除し、使ったメソッド名を返す。"""
    if RELEASE_METHOD == "open_loop":
        dev.SetPositionControlMode(PiezoControlModeTypes.OpenLoop)
        return "SetPositionControlMode(OpenLoop)"

    if RELEASE_METHOD == "stop":
        errors = []
        for name in STOP_METHOD_CANDIDATES:
            fn = getattr(dev, name, None)
            if fn is None:
                continue
            try:
                fn()
                return name
            except Exception as e:      # 引数違いのオーバーロード等
                errors.append(f"{name}: {e}")
        methods = [m for m in dir(dev) if "stop" in m.lower()]
        raise RuntimeError(
            "停止メソッドを呼べませんでした。\n"
            f"  試行: {errors}\n  stop を含むメソッド: {methods}\n"
            "  STOP_METHOD_CANDIDATES を修正してください。")

    raise ValueError(RELEASE_METHOD)


def restore_closed_loop(dev):
    """次の位置決めの前に閉ループへ戻す。"""
    if RELEASE_METHOD == "open_loop":
        dev.SetPositionControlMode(PiezoControlModeTypes.CloseLoop)
        time.sleep(0.5)
    # "stop" の場合は次の SetClosedLoopTarget + MoveStart で再び閉ループ制御される


# ---------------------------------------------------------------- 記録
def record(dev, w, cycle, target_deg, phase, duration_s, t_release, ref_counts, out):
    """duration_s 秒間記録する。t_release: 解除時刻（perf_counter）、
    ref_counts: ドリフトの基準（解除前平均）。out にサンプルを追加して返す。"""
    target_arcsec = target_deg * ARCSEC_PER_DEG
    t_end = time.perf_counter() + duration_s
    while (now := time.perf_counter()) < t_end:
        c = read_counts(dev)
        rel = counts_to_rel_arcsec(c)
        t_rel = now - t_release if t_release is not None else float("nan")
        drift = (c - ref_counts) * ARCSEC_PER_COUNT if ref_counts is not None else float("nan")
        st = read_status_bits(dev)
        w.writerow([stamp(), cycle, target_deg, phase, f"{t_rel:.4f}", c,
                    f"{counts_to_deg(c) - THETA0_DEG:.6f}", f"{rel:.2f}",
                    f"{rel - target_arcsec:.2f}", f"{drift:.2f}",
                    "" if st is None else f"0x{st:08X}"])
        out.append((now, c))
        time.sleep(SAMPLE_MS / 1000)
    return out


def summarize(cycle, target_deg, reached, method, pre, post, t_release):
    """1 サイクル分の評価指標を計算する（単位 arcsec）"""
    pre_counts = [c for _, c in pre]
    ref = statistics.fmean(pre_counts)
    post_t = [t - t_release for t, _ in post]
    post_d = [(c - ref) * ARCSEC_PER_COUNT for _, c in post]

    first = [d for t, d in zip(post_t, post_d) if t <= JUMP_WINDOW_S] or post_d[:1]
    last = [d for t, d in zip(post_t, post_d) if t >= post_t[-1] - END_WINDOW_S]
    jump = statistics.fmean(first)
    end = statistics.fmean(last)
    try:
        slope = statistics.linear_regression(post_t, post_d).slope
    except statistics.StatisticsError:
        slope = float("nan")

    target_arcsec = target_deg * ARCSEC_PER_DEG
    return {
        "cycle": cycle,
        "target_deg": target_deg,
        "reached": int(reached),
        "release_call": method,
        "pre_mean_err_arcsec": round(ref * ARCSEC_PER_COUNT - THETA0_DEG * ARCSEC_PER_DEG - target_arcsec, 3),
        "pre_std_arcsec": round(statistics.pstdev(pre_counts) * ARCSEC_PER_COUNT, 3),
        "jump_arcsec": round(jump, 3),                    # 解除直後の変化
        "drift_arcsec": round(end - jump, 3),             # 解除直後→終盤の変化
        "total_change_arcsec": round(end, 3),             # 解除前→終盤の変化
        "drift_rate_arcsec_per_s": round(slope, 5),
        "post_max_abs_arcsec": round(max(abs(d) for d in post_d), 3),
        "post_std_arcsec": round(statistics.pstdev(post_d), 3),
        "post_pp_arcsec": round(max(post_d) - min(post_d), 3),
        "n_pre": len(pre),
        "n_post": len(post),
    }


def plot_results(curves, path_png):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib がないため図は省略します（uv add matplotlib）")
        return
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for cycle, tgt, ts, ds in curves:
        ax.plot(ts, ds, lw=0.8, label=f"#{cycle} {tgt}°")
    ax.axvline(0, color="k", lw=0.6, ls="--")
    ax.set_xlabel("Time from closed-loop release [s]")
    ax.set_ylabel("Change from hold position [arcsec]")
    ax.grid(alpha=0.3)
    if len(curves) <= 12:
        ax.legend(fontsize=7, ncol=2)
    fig.tight_layout()
    fig.savefig(path_png, dpi=150)
    print("Saved:", path_png)


# ---------------------------------------------------------------- メイン
def run_release_test(dev, path):
    summaries, curves = [], []
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wallclock", "cycle", "target_deg", "phase", "t_from_release_s", "counts",
                    "angle_rel_deg", "angle_rel_arcsec", "dev_from_target_arcsec",
                    "drift_from_hold_arcsec", "status_hex"])

        print(f"開始位置 {START_DEG}° へ移動...")
        move_to_deg(dev, START_DEG)
        time.sleep(SETTLE_S)

        for i, tgt in enumerate(TARGETS_DEG):
            restore_closed_loop(dev)
            ok = move_to_deg(dev, tgt)
            time.sleep(SETTLE_S)

            # 解除前（閉ループ保持）
            pre = record(dev, w, i, tgt, "pre", PRE_S, None, None, [])
            ref = round(statistics.fmean(c for _, c in pre))

            # 閉ループ解除
            t_release = time.perf_counter()
            method = release_closed_loop(dev)
            w.writerow([stamp(), i, tgt, "release", "0.0000", "", "", "", "", "", ""])

            # 解除後
            post = record(dev, w, i, tgt, "post", HOLD_S, t_release, ref, [])
            f.flush()

            s = summarize(i, tgt, ok, method, pre, post, t_release)
            summaries.append(s)
            curves.append((i, tgt,
                           [t - t_release for t, _ in pre + post],
                           [(c - ref) * ARCSEC_PER_COUNT for _, c in pre + post]))
            print(f"[{i + 1}/{len(TARGETS_DEG)}] target {tgt}° ({method}) "
                  f"jump {s['jump_arcsec']:+.2f}\"  drift {s['drift_arcsec']:+.2f}\"  "
                  f"rate {s['drift_rate_arcsec_per_s']:+.4f}\"/s  σ {s['post_std_arcsec']:.2f}\"")

    restore_closed_loop(dev)
    return summaries, curves


def write_summary(summaries, path):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summaries[0].keys()))
        w.writeheader()
        w.writerows(summaries)
    print("Saved:", path)

    for key in ("jump_arcsec", "drift_arcsec", "total_change_arcsec", "post_std_arcsec"):
        vals = [s[key] for s in summaries]
        sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
        print(f"  {key:22s} mean {statistics.fmean(vals):+8.3f}  σ {sd:7.3f}  "
              f"max|x| {max(abs(v) for v in vals):7.3f}  [arcsec]")


def main():
    dev = connect()
    try:
        setup_closed_loop(dev)
        base = f"pdxr1_release_{datetime.now():%Y%m%d_%H%M%S}"
        summaries, curves = run_release_test(dev, base + ".csv")
        print("Saved:", base + ".csv")
        write_summary(summaries, base + "_summary.csv")
        if PLOT:
            plot_results(curves, base + ".png")
    finally:
        dev.StopPolling()
        dev.Disconnect(True)
        print("Disconnected.")


if __name__ == "__main__":
    main()