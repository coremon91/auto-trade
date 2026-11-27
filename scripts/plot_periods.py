from pathlib import Path
import pandas as pd, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from periods import PERIODS
ROOT = Path(__file__).resolve().parents[1]
d = pd.read_csv(ROOT/"data/btcusdt_1d.csv", index_col=0, parse_dates=True).loc["2023-01-01":]
colors = {"1_bear":"#d62728","2_bull":"#2ca02c","3_halving":"#1f77b4","4_range":"#ff7f0e"}
labels = {"1_bear":"1 Bear","2_bull":"2 Bull","3_halving":"3 Post-halving","4_range":"4 Range"}
fig, axes = plt.subplots(2,1,figsize=(13,7),sharex=True,gridspec_kw={"height_ratios":[3,1]})
ax=axes[0]; ax.semilogy(d.index,d.close,color="black",lw=1)
for k,p in PERIODS.items():
    off = 0.0 if k!="3_halving" else 0
    ax.axvspan(pd.Timestamp(p["start"]),pd.Timestamp(p["end"]),color=colors[k],alpha=0.18 if k!="3_halving" else 0.12,label=labels[k])
ax.axvline(pd.Timestamp("2024-04-20"),color="#1f77b4",ls="--",lw=1); ax.text(pd.Timestamp("2024-04-25"),25000,"Halving",color="#1f77b4")
ax.set_ylabel("BTCUSDT close (log)"); ax.legend(loc="upper left"); ax.grid(alpha=.3)
ax.set_title("BTC market regimes for backtesting")
for i,(k,p) in enumerate(PERIODS.items()):
    axes[1].barh(i,(pd.Timestamp(p["end"])-pd.Timestamp(p["start"])).days,left=pd.Timestamp(p["start"]),color=colors[k])
axes[1].set_yticks(range(4)); axes[1].set_yticklabels(labels.values()); axes[1].invert_yaxis(); axes[1].grid(alpha=.3)
plt.tight_layout(); plt.savefig(ROOT/"data/periods.png",dpi=130)
