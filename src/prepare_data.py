from pathlib import Path

import pandas as pd

# raw files (original challenge tables) are read from data/raw
# the four files for Tableau are saved in data
data = Path(__file__).parent.parent / "data"
raw = data / "raw"

files = ["dim_user", "dim_country", "dim_track", "dim_genre", "dim_device",
         "dim_subscription_plan", "fact_listening_session", "fact_subscription_event"]
missing = [f + ".csv" for f in files if not (raw / (f + ".csv")).exists()]
if missing:
    raise SystemExit(f"missing in {raw}: {missing}\n"
                     "copy these csv files from the data folder of the challenge zip")

users = pd.read_csv(raw / "dim_user.csv")
countries = pd.read_csv(raw / "dim_country.csv")
tracks = pd.read_csv(raw / "dim_track.csv")
genres = pd.read_csv(raw / "dim_genre.csv")
devices = pd.read_csv(raw / "dim_device.csv")
plans = pd.read_csv(raw / "dim_subscription_plan.csv")
sessions = pd.read_csv(raw / "fact_listening_session.csv", parse_dates=["listen_start_ts"])
events = pd.read_csv(raw / "fact_subscription_event.csv", parse_dates=["event_ts"])


# ---------- users: extra columns used by the other files ----------
ages = [0, 24, 34, 44, 54, 64, 120]
labels = ["Under 25", "25-34", "35-44", "45-54", "55-64", "65+"]
users["age_band"] = pd.cut(users["age"], ages, labels=labels).astype(str)
users["user_flag"] = users["is_fraud_cluster"].map({True: "Fraud-flagged", False: "Not flagged"})
users = users.merge(countries[["country_code", "country_name"]], on="country_code")
users = users.merge(plans[["plan_id", "monthly_price_usd"]], on="plan_id")


# ---------- file 1: listening sessions ----------
s = sessions.merge(users[["user_id", "user_flag"]], on="user_id")
s = s.merge(countries[["country_code", "country_name"]], on="country_code")
s = s.merge(genres, on="genre_id")
s = s.merge(devices[["device_id", "device_type"]], on="device_id")
s = s.merge(tracks[["track_id", "is_algorithmic_recommendation"]], on="track_id")

s["session_date"] = s["listen_start_ts"].dt.date
s["hour"] = s["listen_start_ts"].dt.hour
s["track_source"] = s["is_algorithmic_recommendation"].map({True: "Algorithmic", False: "Not algorithmic"})
s["skipped"] = s["skipped"].astype(int)
s["new_artist"] = s["new_artist_discovered"].astype(int)
s = s.rename(columns={"subscription_tier": "tier", "country_name": "country", "genre_name": "genre"})

# month, year and weekday are left out, Tableau can make them from session_date
cols = ["session_id", "user_id", "session_date", "hour", "tier", "country", "device_type", "genre",
        "track_source", "listen_seconds", "skipped", "new_artist", "estimated_revenue_usd", "user_flag"]
s[cols].to_csv(data / "streaming_sessions.csv", index=False)


# ---------- file 2: subscription events ----------
e = events.sort_values(["user_id", "event_ts", "event_id"]).copy()
e = e.merge(users[["user_id", "user_flag", "age_band"]], on="user_id")
e = e.merge(countries[["country_code", "country_name"]], on="country_code")

e["month"] = e["event_ts"].dt.to_period("M")
e["event_date"] = e["event_ts"].dt.date
e["event_month"] = e["month"].dt.to_timestamp().dt.date
e["year"] = e["event_ts"].dt.year

# every event type gets a name for the MRR bridge
e["mrr_movement"] = e["event_type"].map({"signup": "New", "upgrade": "Expansion", "downgrade": "Contraction",
                                         "churn": "Churn", "retention": "Retained"})

# a paid user leaves = churn from Premium or Family, or a downgrade to Free
paid_churn = (e["event_type"] == "churn") & e["from_tier"].isin(["Premium", "Family"])
drop_to_free = (e["event_type"] == "downgrade") & (e["to_tier"] == "Free")
e["is_paid_exit"] = (paid_churn | drop_to_free).astype(int)

# plan changes made by the system, not by the customer
e["is_reconciliation"] = (e["trigger_context"] == "reconciliation").astype(int)

e = e.rename(columns={"country_name": "country"})
cols = ["event_id", "user_id", "event_date", "event_month", "year", "event_type", "mrr_movement", "from_tier",
        "to_tier", "mrr_before_usd", "mrr_after_usd", "mrr_change_usd", "is_paid_exit", "is_reconciliation",
        "country", "trigger_context", "user_flag", "age_band"]
e[cols].to_csv(data / "streaming_subscription_events.csv", index=False)


# ---------- file 3: one row per month ----------
months = pd.period_range("2021-01", "2024-12", freq="M")

# MRR of every user at the end of each month: take the last value of the month,
# and for months without events keep the value of the month before
mrr_by_user = e.groupby(["user_id", "month"])["mrr_after_usd"].last().unstack()
mrr_by_user = mrr_by_user.reindex(columns=months).ffill(axis=1).fillna(0)

# same idea for the tier
tier_by_user = e.groupby(["user_id", "month"])["to_tier"].last().unstack()
tier_by_user = tier_by_user.reindex(columns=months).ffill(axis=1)

m = pd.DataFrame(index=months)
m["mrr_end"] = mrr_by_user.sum()
m["paid_users_end"] = (mrr_by_user > 0).sum()
m["premium_users_end"] = (tier_by_user == "Premium").sum()
m["family_users_end"] = (tier_by_user == "Family").sum()
m["paid_users_start"] = m["paid_users_end"].shift(1).fillna(0).astype(int)

# MRR change per month, split by type
moves = e.groupby(["month", "mrr_movement"])["mrr_change_usd"].sum().unstack()
moves = moves.reindex(months).fillna(0)
m["new_mrr"] = moves["New"]
m["expansion_mrr"] = moves["Expansion"]
m["contraction_mrr"] = moves["Contraction"]
m["churn_mrr"] = moves["Churn"]

# paid users who left, and the same count without the system changes
m["paid_exits"] = e.groupby("month")["is_paid_exit"].sum().reindex(months).fillna(0).astype(int)
m["paid_exit_rate_pct"] = (100 * m["paid_exits"] / m["paid_users_start"]).where(m["paid_users_start"] > 0).round(2)

e["recon_mrr"] = e["mrr_change_usd"].where(e["is_reconciliation"] == 1, 0)
m["reconciliation_mrr"] = e.groupby("month")["recon_mrr"].sum().reindex(months).fillna(0)

no_recon = e[e["is_reconciliation"] == 0]
m["paid_exits_excl_reconciliation"] = no_recon.groupby("month")["is_paid_exit"].sum().reindex(months).fillna(0).astype(int)

# listening per month
sessions["month"] = sessions["listen_start_ts"].dt.to_period("M")
m["sessions"] = sessions.groupby("month").size().reindex(months).fillna(0).astype(int)
m["active_users"] = sessions.groupby("month")["user_id"].nunique().reindex(months).fillna(0).astype(int)

# accounts that existed by the end of each month
users["created_month"] = pd.to_datetime(users["account_created"]).dt.to_period("M")
m["accounts_total"] = [(users["created_month"] <= month).sum() for month in months]

m.index = m.index.to_timestamp().date
m.index.name = "month"
m.round(2).to_csv(data / "streaming_monthly_mrr.csv")


# ---------- file 4: one row per user ----------
per_user = sessions.groupby("user_id").agg(total_sessions=("session_id", "size"),
                                           skip_rate=("skipped", "mean"),
                                           avg_listen_seconds=("listen_seconds", "mean")).reset_index()
u = users.merge(per_user, on="user_id", how="left")
u["total_sessions"] = u["total_sessions"].fillna(0).astype(int)
u = u.rename(columns={"country_name": "country", "subscription_tier": "current_tier",
                      "monthly_price_usd": "current_mrr_usd"})
cols = ["user_id", "account_created", "age", "age_band", "gender", "country", "current_tier", "current_mrr_usd",
        "user_flag", "total_sessions", "skip_rate", "avg_listen_seconds"]
u[cols].round(3).to_csv(data / "streaming_users.csv", index=False)

print("done, 4 files saved in", data)
