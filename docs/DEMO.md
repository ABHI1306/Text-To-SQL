# 10-minute demo

Audience: someone who has not seen the product. Everything is local; say so early.
Setup before the room: the README quick start done, models pulled, `make up`, the UI open on
Home, signed in as a DBA, and a second browser profile ready for the user sign-in. Expect each
answer to take from several seconds (GPU) to over a minute (CPU): ask the next slide's
question while one is running, or pre-ask the ones marked (warm).

| Min | Show | Say |
| --- | --- | --- |
| 0:00 | **Home**, empty chat | "A local Text-to-SQL service. The model runs on this machine; nothing leaves it." |
| 0:45 | **Setup** > PostgreSQL: host `demo-db`, test connection, pick tables, define relationships, save; preview live rows from **Review**, then **Deploy** | "The DBA can inspect the connected data directly; the assistant uses the reviewed semantic model." |
| 3:00 | **Connect and model**: drag a relationship, add a calculated field, open a metric preview | "The compiler turns questions over this model into SQL, so rules live in one place." |
| 4:00 | **Home**: "How many orders are there per region?" (warm) | Point at the steps timeline, the SQL tabs, the chart and the short answer. "Every step is visible." |
| 5:00 | Click one of the follow-up chips under the answer | "Follow-ups keep the conversation's context." |
| 5:45 | **Row policies**: add `region = {{ user.region }}` on orders, exempt DBA. **Users and roles**: create `maria`, role User, attribute `region=EU`, project access | "Access rules are enforced by the compiler, not by the model's good behaviour." |
| 7:00 | Second browser, sign in as `maria`, ask the same question | Only EU rows; no Setup, Audit or Operations in the menu. "Same words, different data." |
| 8:00 | As the DBA: **Pin to dashboard**, then **Schedule this question** (daily), then **Share** with `maria` | "A schedule runs as its owner, with the owner's access." |
| 8:45 | As `maria`: **Shared with me** | "She sees the dashboard computed for her, read-only." |
| 9:15 | **Audit**: filter by action; **Operations**: tokens and model time per question, scheduler health | "Who asked what, and what it costs on this hardware." |
| 9:45 | Offer `make offline` output (all services blocked from the internet) | "It works with the network cut." |

If something fails live: Operations shows whether the worker or model is down, and
`curl localhost:8000/health/ready` names the broken dependency. A vague question like "show as a
trend" gets a clarifying question back instead of SQL; rephrase it completely.
