# Run: campaign -> walk-forward -> gate evaluation
from research.run_institutional_campaign import run_commands, analyze_results

if __name__ == "__main__":
    results = run_commands(args=None)
    analyze_results(results, Path("research/results/institutional_campaign.json"))
    print("\nLane 2: Gate evaluation")
    print("Done")
