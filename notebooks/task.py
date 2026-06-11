import os
import gc
import time
import tracemalloc
import statistics
from pathlib import Path

import pandas as pd
from pyspark.sql import SparkSession

# ============================================================
# CONFIG
# ============================================================

# RUN_VERSION = "local"
# RUN_VERSION = "dataproc"
RUN_VERSION = "compare"

BUCKET = "tbd-2026l-342189-state"
N_ROWS = 2_000_000
INPUT_SIZE_MB = 204

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

spark = SparkSession.builder.appName("TBD_Task").getOrCreate()
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
# DATAPROC BENCHMARK
# ============================================================

if RUN_VERSION == "dataproc":
    EVENTS_PATH    = f"gs://{BUCKET}/project/events.parquet"
    DIMENSION_PATH = f"gs://{BUCKET}/project/dimension.parquet"

    benchmark_results = (
        pd.read_parquet(DATAPROC_RESULTS_FILE).to_dict(orient="records")
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
        pd.DataFrame(benchmark_results, columns=BENCHMARK_COLUMNS).to_parquet(DATAPROC_RESULTS_FILE, index=False)

    df_results = pd.DataFrame(benchmark_results, columns=BENCHMARK_COLUMNS)
    print("\n=== DATAPROC RESULTS ===")
    print(df_results[["library_engine", "query_name", "median_time_s", "peak_memory_mb", "result_check"]])

    # Zapisz wyniki na GCS przez Spark
    gcs_output = f"gs://{BUCKET}/project/results_pyspark_dataproc.parquet"
    spark.createDataFrame(df_results).write.mode("overwrite").parquet(gcs_output)
    print(f"\nWyniki zapisane na: {gcs_output}")

# ============================================================
# LOCAL BENCHMARK
# ============================================================

elif RUN_VERSION == "local":
    EVENTS_PATH    = "events.parquet"
    DIMENSION_PATH = "dimension.parquet"

    benchmark_results = (
        pd.read_parquet(LOCAL_RESULTS_FILE).to_dict(orient="records")
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
                               notes="PySpark Local")
        benchmark_results.append(result)
        pd.DataFrame(benchmark_results, columns=BENCHMARK_COLUMNS).to_parquet(LOCAL_RESULTS_FILE, index=False)

    df_results = pd.DataFrame(benchmark_results, columns=BENCHMARK_COLUMNS)
    print("\n=== LOCAL RESULTS ===")
    print(df_results[["library_engine", "query_name", "median_time_s", "peak_memory_mb", "result_check"]])

# ============================================================
# COMPARE
# ============================================================

elif RUN_VERSION == "compare":
    if not Path(LOCAL_RESULTS_FILE).exists():
        raise FileNotFoundError(f"Brak lokalnych wyników: {LOCAL_RESULTS_FILE}. Uruchom najpierw RUN_VERSION='local'.")
    if not Path(DATAPROC_RESULTS_FILE).exists():
        raise FileNotFoundError(f"Brak wyników dataproc: {DATAPROC_RESULTS_FILE}. Pobierz z GCS: gsutil cp gs://{BUCKET}/project/{DATAPROC_RESULTS_FILE} .")

    df_local    = pd.read_parquet(LOCAL_RESULTS_FILE)
    df_dataproc = pd.read_parquet(DATAPROC_RESULTS_FILE)
    df_all      = pd.concat([df_local, df_dataproc], ignore_index=True)

    print("\n=== WSZYSTKIE WYNIKI ===")
    print(df_all[["library_engine", "query_name", "median_time_s", "peak_memory_mb", "result_check"]].to_string())

    pivot = df_all.pivot_table(
        index="query_name",
        columns="library_engine",
        values=["median_time_s", "peak_memory_mb"],
        aggfunc="median",
    )
    print("\n=== PORÓWNANIE (pivot) ===")
    print(pivot.to_string())

else:
    raise ValueError("RUN_VERSION must be: 'local', 'dataproc', or 'compare'")