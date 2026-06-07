import os
import gc
import time
import tracemalloc
import statistics
from pathlib import Path

import polars as pl
from pyspark.sql import SparkSession

# ============================================================
# CONFIG
# ============================================================

# RUN_VERSION = "local"
RUN_VERSION = "dataproc"
# RUN_VERSION = "compare"

BUCKET = "tbd-2026l-342189-state"
N_ROWS = 2_000_000
INPUT_SIZE_MB = 45.3  # TODO nieznany dla GCS — zostawiamy 0

LOCAL_RESULTS_FILE  = "results_pyspark_local.parquet"
DATAPROC_RESULTS_FILE = "results_pyspark_dataproc.parquet"

BENCHMARK_COLUMNS = [
    "library_engine",
    "mode",
    "query_name",
    "data_format",
    "layout",
    "rows",
    "median_time_s",
    "peak_memory_mb",
    "input_size_mb",
    "result_check",
    "notes",
]

# ============================================================
# BENCHMARK HELPERS
# ============================================================

def measure_once(fn):
    gc.collect()
    tracemalloc.start()
    start = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return elapsed, peak / 1024 / 1024, result


def run_benchmark(fn, library_engine, query_name, data_format, layout,
                  rows, input_size_mb, mode="eager", n_reps=5, notes=""):
    times, memories = [], []
    result_check = None
    for i in range(n_reps):
        elapsed, peak_mb, result = measure_once(fn)
        times.append(elapsed)
        memories.append(peak_mb)
        if i == 0:
            result_check = result
    return {
        "library_engine":  library_engine,
        "mode":            mode,
        "query_name":      query_name,
        "data_format":     data_format,
        "layout":          layout,
        "rows":            rows,
        "median_time_s":   round(statistics.median(times), 6),
        "peak_memory_mb":  round(statistics.median(memories), 2),
        "input_size_mb":   round(input_size_mb, 2),
        "result_check":    result_check,
        "notes":           notes,
    }

# ============================================================
# SPARK SESSION
# ============================================================

try:
    spark.stop()
except Exception:
    pass

spark = SparkSession.builder.appName("TBD_Task5").getOrCreate()
spark.sparkContext.setLogLevel("ERROR")

# ============================================================
# PYSPARK QUERIES
# ============================================================

def q1_pyspark():
    return spark.read.parquet(EVENTS_PATH).filter(
        "event_date BETWEEN '2026-02-01' AND '2026-02-28' AND country = 'PL'"
    ).groupBy("browser").agg(
        {"time_spent": "avg", "ads_viewed": "sum", "session_id": "count"}
    ).count()


def q2_pyspark():
    from pyspark.sql.functions import explode, countDistinct, avg, count
    return spark.read.parquet(EVENTS_PATH).select(
        "session_id", "visitor_id", "time_spent", explode("tags").alias("tag")
    ).groupBy("tag").agg(
        count("session_id").alias("session_count"),
        countDistinct("visitor_id").alias("unique_visitors"),
        avg("time_spent").alias("avg_time_spent"),
    ).orderBy("session_count", ascending=False).count()


def q3_pyspark():
    from pyspark.sql.functions import round as spark_round, sum as spark_sum, count
    df  = spark.read.parquet(EVENTS_PATH)
    dim = spark.read.parquet(DIMENSION_PATH)
    return df.join(dim, on="page_url", how="left").groupBy(
        "page_section", "country"
    ).agg(
        spark_round(spark_sum("time_spent"), 3).alias("total_time_spent"),
        count("session_id").alias("session_count"),
    ).orderBy("session_count", ascending=False).count()


spark_runs = [
    (q1_pyspark, "Q1_filter_agg"),
    (q2_pyspark, "Q2_explode_groupby"),
    (q3_pyspark, "Q3_join_dimension"),
]

# ============================================================
# LOCAL BENCHMARK
# ============================================================

if RUN_VERSION == "local":
    EVENTS_PATH    = "../data/phase2_26L/group_10/events.parquet"
    DIMENSION_PATH = "../data/phase2_26L/group_10/dimension.parquet"

    benchmark_results = (
        pl.read_parquet(LOCAL_RESULTS_FILE).to_dicts()
        if Path(LOCAL_RESULTS_FILE).exists() else []
    )
    existing = {(r["library_engine"], r["query_name"]) for r in benchmark_results}

    for fn, qname in spark_runs:
        key = ("pyspark_local", qname)
        if key in existing:
            print(f"SKIP {key}")
            continue
        print(f"RUN {key}")
        result = run_benchmark(fn, "pyspark_local", qname, "parquet", "default",
                               N_ROWS, INPUT_SIZE_MB, mode="sql", n_reps=5,
                               notes="PySpark local[*]")
        benchmark_results.append(result)
        pl.DataFrame(benchmark_results, schema=BENCHMARK_COLUMNS, orient="row").write_parquet(LOCAL_RESULTS_FILE)

    print("\n=== LOCAL RESULTS ===")
    print(pl.DataFrame(benchmark_results, schema=BENCHMARK_COLUMNS, orient="row")
          .select(["library_engine", "query_name", "median_time_s", "peak_memory_mb", "result_check"]))

# ============================================================
# DATAPROC BENCHMARK
# ============================================================

elif RUN_VERSION == "dataproc":
    EVENTS_PATH    = f"gs://{BUCKET}/project/events.parquet"
    DIMENSION_PATH = f"gs://{BUCKET}/project/dimension.parquet"

    benchmark_results = (
        pl.read_parquet(DATAPROC_RESULTS_FILE).to_dicts()
        if Path(DATAPROC_RESULTS_FILE).exists() else []
    )
    existing = {(r["library_engine"], r["query_name"]) for r in benchmark_results}

    for fn, qname in spark_runs:
        key = ("pyspark_dataproc", qname)
        if key in existing:
            print(f"SKIP {key}")
            continue
        print(f"RUN {key}")
        result = run_benchmark(fn, "pyspark_dataproc", qname, "parquet", "default",
                               N_ROWS, INPUT_SIZE_MB, mode="sql", n_reps=5,
                               notes="PySpark Dataproc")
        benchmark_results.append(result)
        pl.DataFrame(benchmark_results, schema=BENCHMARK_COLUMNS, orient="row").write_parquet(DATAPROC_RESULTS_FILE)

    print("\n=== DATAPROC RESULTS ===")
    print(pl.DataFrame(benchmark_results, schema=BENCHMARK_COLUMNS, orient="row")
          .select(["library_engine", "query_name", "median_time_s", "peak_memory_mb", "result_check"]))

# ============================================================
# COMPARISON TABLE
# ============================================================

elif RUN_VERSION == "compare":
    if not Path(LOCAL_RESULTS_FILE).exists():
        raise FileNotFoundError(f"Missing {LOCAL_RESULTS_FILE}")
    if not Path(DATAPROC_RESULTS_FILE).exists():
        raise FileNotFoundError(f"Missing {DATAPROC_RESULTS_FILE}")

    local   = pl.read_parquet(LOCAL_RESULTS_FILE)
    dataproc = pl.read_parquet(DATAPROC_RESULTS_FILE)

    report = (
        local.select(["query_name", pl.col("median_time_s").alias("local_time_s"),
                      pl.col("peak_memory_mb").alias("local_memory_mb")])
        .join(dataproc.select(["query_name", pl.col("median_time_s").alias("dataproc_time_s"),
                                pl.col("peak_memory_mb").alias("dataproc_memory_mb")]),
              on="query_name", how="inner")
        .with_columns((pl.col("local_time_s") / pl.col("dataproc_time_s")).round(2).alias("speedup"))
        .sort("query_name")
    )

    print("\n=== REPORT TABLE ===")
    print(report)
    report.write_csv("pyspark_local_vs_dataproc_report.csv")

else:
    raise ValueError("RUN_VERSION must be: 'local', 'dataproc', or 'compare'")