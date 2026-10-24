"""Streamlit dashboard: streamlit run price_stalker/dashboard/app.py"""
import pandas as pd
import streamlit as st

from price_stalker.database.store import Store

st.set_page_config(page_title="Price Stalker", layout="wide")
st.title("Price Stalker")

store = Store()
products = store.products()
if not products:
    st.info("Nothing tracked yet. Run `price-stalker add \"Name\" <url>` then `price-stalker check`.")
    st.stop()

product = st.sidebar.selectbox("Product", products)
rows = store.history(product)
if not rows:
    st.warning("No prices recorded yet. Run `price-stalker check`.")
    st.stop()

df = pd.DataFrame([dict(r) for r in rows])
df["price"] = df["price_cents"] / 100
df["series"] = df["retailer"] + df["variant"].map(lambda v: f" · {v}" if v else "")
df["checked_at"] = pd.to_datetime(df["checked_at"])

latest = df.sort_values("checked_at").groupby("series").tail(1).sort_values("price")
best = latest.iloc[0]
c1, c2 = st.columns(2)
c1.metric("Best current price", f"${best['price']:,.2f}", best["series"])
c2.metric("Retailers tracked", len(latest))

st.subheader("Price history")
st.line_chart(df.pivot_table(index="checked_at", columns="series", values="price", aggfunc="last").ffill())

st.subheader("Latest by retailer")
st.dataframe(latest[["series", "price", "currency", "in_stock", "checked_at", "url"]],
             hide_index=True, width="stretch",
             column_config={"url": st.column_config.LinkColumn("link")})
