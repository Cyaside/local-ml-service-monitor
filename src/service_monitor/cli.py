import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="CPU-only telemetry anomaly detection pipeline")
    commands = parser.add_subparsers(dest="command", required=True)
    from .runtime_cli import register, dispatch, COMMANDS
    register(commands)
    p = commands.add_parser("generate-synthetic")
    p.add_argument("--out", required=True)
    p.add_argument("--rows", type=int, default=1200)
    p.add_argument("--seed", type=int, default=42)
    p = commands.add_parser("train-foundation")
    p.add_argument("--cache", default="data/public")
    p.add_argument("--out", required=True)
    p.add_argument("--profile", choices=["quick", "full"], default="full")
    p.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    p = commands.add_parser("calibrate")
    p.add_argument("--foundation", required=True)
    p.add_argument("--baseline", required=True)
    p.add_argument("--validation")
    p.add_argument("--out", required=True)
    p.add_argument("--threshold-quantile", type=float, default=.995)
    p = commands.add_parser("evaluate")
    p.add_argument("--model", required=True)
    for option in ("input", "report"):
        p.add_argument("--" + option, required=True)
    p.add_argument("--allow-synthetic", action="store_true")
    p = commands.add_parser("predict")
    p.add_argument("--model", required=True)
    for option in ("input", "out"):
        p.add_argument("--" + option, required=True)
    p.add_argument("--allow-synthetic", action="store_true")
    p = commands.add_parser("inspect-data")
    p.add_argument("--input", required=True)
    args = parser.parse_args()
    if args.command in COMMANDS:
        import httpx
        import sqlite3
        try:
            dispatch(args)
        except KeyboardInterrupt:
            print("Stopped.")
        except (ValueError, OSError, sqlite3.Error, httpx.HTTPError) as exc:
            parser.exit(2, f"error: {exc}\n")
        return
    from .ml.synthetic import generate
    from .ml.training import calibrate, read_data, Predictor, evaluate_model
    from .ml.features import prepare, reference_from_frame
    try:
        if args.command == "generate-synthetic":
            generate(args.out, args.rows, args.seed)
            print("Generated development-only synthetic splits")
        elif args.command == "train-foundation":
            from .ml.public_training import train_public_foundation
            result = train_public_foundation(args.cache, args.out, args.profile, args.device)
            print(json.dumps({"foundation": args.out, "validation": result["validation"],
                              "training_seconds": result["training_seconds"]}, indent=2))
        elif args.command == "calibrate":
            result = calibrate(args.foundation, args.baseline, args.out,
                               args.threshold_quantile, args.validation)
            print(json.dumps({"model": args.out,
                              "calibration_seconds": result["calibration_seconds"],
                              "threshold": result["threshold"]}, indent=2))
        elif args.command == "evaluate":
            evaluate_model(args.model, args.input, args.report, args.allow_synthetic)
            print(f"Report saved: {args.report}")
        elif args.command == "inspect-data":
            frame = read_data(args.input)
            x, _ = prepare(frame, reference_from_frame(frame))
            print(json.dumps({"raw_rows": len(frame), "feature_rows": len(x),
                              "excluded_or_warmup": len(frame)-len(x)}))
        elif args.command == "predict":
            predictor = Predictor(args.model, args.allow_synthetic)
            frame = read_data(args.input)
            if set(frame.service_id) != {predictor.metadata["service_id"]}:
                raise ValueError("Service does not match model")
            x, indices = prepare(frame, predictor.reference)
            scores, flags = predictor.score(x)
            result = frame[["timestamp", "service_id", "run_id", "seq"]].copy()
            result["anomaly_score"] = float("nan")
            result["is_anomaly"] = None
            result["prediction_status"] = "WARMUP_OR_INVALID"
            result.loc[indices, "anomaly_score"] = scores
            result.loc[indices, "is_anomaly"] = flags.tolist()
            result.loc[indices, "prediction_status"] = "SCORED"
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            result.to_csv(args.out, index=False)
            print(f"Predictions saved: {args.out}")
    except (ValueError, FileNotFoundError, FileExistsError, KeyError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
