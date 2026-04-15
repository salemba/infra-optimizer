"""
Entry point — API server or CLI.

  python main.py                              # start API server
  python main.py --file data/metrics.json     # analyze from CLI, no server
  python main.py --vendor-assets              # download Chart.js for on-premise

M4 fix: structured JSON logging configured here — all modules inherit this setup.
M6 fix: removed unused 'sys' import.
S7 fix: --vendor-assets downloads Chart.js so the dashboard works without CDN.
"""
import argparse
import json
import logging
import os

from dotenv import load_dotenv

load_dotenv()


# ── M4: Structured logging setup ──────────────────────────────────────────
# JSON format lets log aggregators (ELK, Loki, Datadog) parse fields directly.
# Falls back to plain text if python-json-logger is not installed.

def _setup_logging() -> None:
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    try:
        from pythonjsonlogger import jsonlogger
        handler = logging.StreamHandler()
        handler.setFormatter(jsonlogger.JsonFormatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s"
        ))
        logging.basicConfig(level=level, handlers=[handler], force=True)
    except ImportError:
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        )

_setup_logging()
logger = logging.getLogger(__name__)


# ── Modes ──────────────────────────────────────────────────────────────────

def serve() -> None:
    import uvicorn
    uvicorn.run(
        "app.api:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", 8000)),
        reload=os.getenv("ENV", "prod") == "dev",
    )


def cli(metrics_file: str, output_file: str, is_prediction: bool  ) -> None:
    from app.models import AnalyzeRequest, PredictRequest, MetricPoint
    from app.predective.predict import predict
    from app.graph import run
    logger.info("cli_start", extra={"metrics_file": metrics_file, "output_file": output_file, "predict": predict})
    with open(metrics_file, encoding="utf-8") as f:
        raw = json.load(f)

    if not is_prediction:

        request = AnalyzeRequest(metrics=[MetricPoint(**p) for p in raw])
        report  = run(request)
        os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(report.model_dump(), f, indent=2, ensure_ascii=False, default=str)
    else:
        # Prediction mode: compress history into a statistical summary prompt
        # (avoids sending the full dataset to the LLM).
        from app.predective.predict import build_prompt, predict as run_predict
        metrics = [MetricPoint(**p) for p in raw]
        prompt  = build_prompt(metrics)
        result  = run_predict(prompt)
        report  = result.model_dump() if result else {"error": "prediction failed — check logs"}
        os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        if result:
            sev = result.severity.upper()
            recs = len(result.recommendations)
            logger.info("cli_predict_done", extra={"severity": result.severity,
                        "target": result.target_timestamp, "recs": recs, "output": output_file})
            print(f"[PREDICT {sev}] {result.target_timestamp} — {recs} recommandation(s) → {output_file}")
        else:
            print(f"[PREDICT ERROR] prediction failed — see logs")
        return

    logger.info("cli_report", extra={"output": output_file})
    s = report.summary
    logger.info("cli_done", extra={
        "health":   s.overall_health,
        "anomalies": s.anomaly_count,
        "critical":  s.critical_count,
        "output":    output_file,
    })
    print(f"[{s.overall_health.upper()}] {s.anomaly_count} anomalies "
          f"({s.critical_count} contriques) → {output_file}")


def vendor_assets() -> None:
    """
    S7: Download Chart.js for on-premise use (no CDN dependency).
    Run once after cloning: python main.py --vendor-assets
    """
    import urllib.request
    from pathlib import Path

    url    = "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"
    target = Path("dashboard/static/chart.umd.min.js")
    target.parent.mkdir(parents=True, exist_ok=True)

    logger.info("vendor_download", extra={"url": url, "target": str(target)})
    urllib.request.urlretrieve(url, target)
    print(f"Chart.js vendored → {target}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="InfraOptimizer")
    parser.add_argument("--file",          help="Analyze a metrics JSON file (CLI mode)")
    parser.add_argument("--out",           default="output/report.json")
    parser.add_argument("--vendor-assets", action="store_true",
                        help="Download Chart.js for on-premise use")
    parser.add_argument("--predict", default=False, action=argparse.BooleanOptionalAction, help="Run prediction instead of analysis")
    args = parser.parse_args()

    if args.vendor_assets:
        vendor_assets()
    elif args.file:
        cli(args.file, args.out, args.predict)
    else:
        serve()
