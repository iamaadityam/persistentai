import time
import os
import uuid
from datetime import datetime, timezone
from sarvamai import SarvamAI
import streamlit as st
from dotenv import load_dotenv
from neo4j import GraphDatabase
from google import genai

load_dotenv()

st.set_page_config(
    page_title="MemoryDesk AI",
    page_icon="🧠",
    layout="wide"
)

# ---------- CONNECTIONS ----------

@st.cache_resource
def get_driver():
    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(
            os.environ["NEO4J_USERNAME"],
            os.environ["NEO4J_PASSWORD"]
        )
    )
    driver.verify_connectivity()
    return driver


@st.cache_resource
@st.cache_resource
def get_ai():
    api_key = os.getenv("SARVAM_API_KEY")

    if not api_key:
        raise ValueError("SARVAM_API_KEY is missing from .env")

    return SarvamAI(api_subscription_key=api_key)


driver = get_driver()
ai = get_ai()


# ---------- PERSISTENT MEMORY ----------

def report_issue(customer_id, customer_name, product, description):
    ticket_id = "T-" + uuid.uuid4().hex[:8].upper()
    issue_id = "I-" + uuid.uuid4().hex[:8].upper()
    product_id = "P-" + uuid.uuid4().hex[:8].upper()
    resolution_id = "R-" + uuid.uuid4().hex[:8].upper()
    now = datetime.now(timezone.utc).isoformat()

    query = """
    MERGE (c:Customer {id: $customer_id})
    SET c.name = $customer_name

    CREATE (p:Product {
        id: $product_id, name: $product
    })
    CREATE (i:Issue {
        id: $issue_id,
        description: $description,
        created_at: $now
    })
    CREATE (t:SupportTicket {
        id: $ticket_id,
        status: 'open',
        attempted: [],
        created_at: $now
    })
    CREATE (r:Resolution {
        id: $resolution_id,
        status: 'unresolved',
        next_step: 'Investigate the issue'
    })

    MERGE (c)-[:OWNS]->(p)
    MERGE (c)-[:REPORTED]->(i)
    MERGE (i)-[:TRACKED_IN]->(t)
    MERGE (t)-[:HAS_RESOLUTION]->(r)

    RETURN t.id AS ticket_id
    """

    with driver.session() as session:
        result = session.run(
            query,
            customer_id=customer_id,
            customer_name=customer_name,
            product_id=product_id,
            product=product,
            issue_id=issue_id,
            description=description,
            ticket_id=ticket_id,
            resolution_id=resolution_id,
            now=now
        )
        record = result.single()
        if record is None:
            raise ValueError("Failed to create support ticket in Neo4j.")
        return record["ticket_id"]


def retrieve_history(customer_id):
    with driver.session() as session:
        tickets = session.run("""
            MATCH (c:Customer {id: $cid})
                  -[:REPORTED]->(i:Issue)
                  -[:TRACKED_IN]->(t:SupportTicket)

            WITH DISTINCT c, i, t

            OPTIONAL MATCH (t)-[:HAS_RESOLUTION]->(r:Resolution)

            WITH DISTINCT i, t, r

            OPTIONAL MATCH (c:Customer {id: $cid})
                          -[:OWNS]->(p:Product)

            WITH i, t, r, head(collect(DISTINCT p.name)) AS product

            RETURN DISTINCT
                   i.description AS issue,
                   i.created_at AS date,
                   product AS product,
                   t.id AS ticket,
                   t.status AS status,
                   t.attempted AS attempted,
                   r.status AS resolution_status,
                   r.next_step AS next_step

            ORDER BY date DESC
            LIMIT 10
        """, cid=customer_id).data()

        interactions = session.run("""
            MATCH (c:Customer {id: $cid})
                  -[:HAD_INTERACTION]->(m:Interaction)
            RETURN m.role AS role,
                   m.content AS content,
                   m.created_at AS created_at
            ORDER BY created_at DESC
            LIMIT 12
        """, cid=customer_id).data()

    interactions.reverse()
    return tickets, interactions


def save_interaction(customer_id, role, content):
    with driver.session() as session:
        session.run("""
            MATCH (c:Customer {id: $cid})
            CREATE (m:Interaction {
                id: $id,
                role: $role,
                content: $content,
                created_at: $created_at
            })
            CREATE (c)-[:HAD_INTERACTION]->(m)
        """,
            cid=customer_id,
            id=uuid.uuid4().hex,
            role=role,
            content=content,
            created_at=datetime.now(timezone.utc).isoformat()
        )


import json




def generate_with_retry(prompt, max_attempts=2):
    import time

    client = get_ai()

    for attempt in range(max_attempts):
        try:
            response = client.chat.completions(
                model="sarvam-105b-conversations",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a helpful customer support agent. "
                            "Always return a useful text response."
                        ),
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                reasoning_effort=None,
                max_tokens=1200,
                temperature=0.2,
            )

            content = response.choices[0].message.content

            if content and content.strip():
                return content.strip()

            raise ValueError(
                f"Sarvam returned empty content: {response}"
            )

        except Exception:
            if attempt == max_attempts - 1:
                raise
            time.sleep(1)

    raise RuntimeError("Sarvam generation failed")

def extract_confirmed_attempts(message):
    prompt = f"""
Extract troubleshooting actions the customer explicitly says
they have already performed.

Return ONLY a JSON array of short strings.
Examples:
"I restarted Bluetooth, but it still fails."
=> ["Restarted laptop Bluetooth"]

"I will restart Bluetooth."
=> []

"Should I reset my earbuds?"
=> []

"The agent told me to restart Bluetooth, but I haven't."
=> []

Do not include suggestions, questions, intentions, or actions
that another person performed.

Customer message:
{json.dumps(message)}
"""

    text = generate_with_retry(prompt)

    try:
        attempts = json.loads(text or "[]")
        if not isinstance(attempts, list):
            return []
        return [
            item.strip()
            for item in attempts
            if isinstance(item, str) and item.strip()
        ]
    except (json.JSONDecodeError, TypeError):
        return []

def normalize_attempt(attempt):
    text = " ".join(attempt.lower().strip().split())

    aliases = {
        "restart bluetooth": "Restarted laptop Bluetooth",
        "restarted bluetooth": "Restarted laptop Bluetooth",
        "restart bluetooth on laptop": "Restarted laptop Bluetooth",
        "restarted bluetooth on laptop": "Restarted laptop Bluetooth",
        "restart laptop bluetooth": "Restarted laptop Bluetooth",
        "restarted laptop bluetooth": "Restarted laptop Bluetooth",
    }

    return aliases.get(text, attempt.strip())

def record_attempt(customer_id, attempt):
    attempt = normalize_attempt(attempt)
    with driver.session() as session:
        result = session.run("""
            MATCH (c:Customer {id: $cid})
                  -[:REPORTED]->(i:Issue)
                  -[:TRACKED_IN]->(t:SupportTicket)
            WHERE t.status = 'open'

            WITH t
            ORDER BY coalesce(t.created_at, '') DESC
            LIMIT 1

            SET t.attempted =
                CASE
                    WHEN $attempt IN coalesce(t.attempted, [])
                    THEN coalesce(t.attempted, [])
                    ELSE coalesce(t.attempted, []) + $attempt
                END

            RETURN t.id AS ticket_id,
                   t.attempted AS attempted
        """, cid=customer_id, attempt=attempt)

        return result.single()

# ---------- AI RESPONSE ----------


def generate_reply(message, ticket_context, conversation_context):
    prompt = f"""
You are MemoryDesk, a careful customer-support agent.

CURRENT CUSTOMER MESSAGE:
{message}

SUPPORT TICKETS RETRIEVED FROM NEO4J:
{ticket_context}

PREVIOUS INTERACTIONS RETRIEVED FROM NEO4J:
{conversation_context}

STRICT RULES:
1. Treat the retrieved records as the source of truth.
2. Never say the customer tried a step unless it appears explicitly
   in the ticket's attempted list or saved conversation interactions.
3. Distinguish between a suggested step and a completed step.
4. If attempted is empty, say no troubleshooting attempts are
   recorded in the ticket. Do not invent any.
5. Never invent ticket IDs, ticket statuses, product specifications,
   or previous conversations.
6. Acknowledge the current ticket status accurately.
7. Suggest one or two safe next steps. If a product-specific reset
   procedure is uncertain, ask for the exact model or recommend
   checking official product instructions.
8. Ask a clarifying question when necessary.
9. Keep your response concise, friendly, and actionable.
10. Do not claim that you changed database records unless the
    application actually performed that update.

11. Treat customer messages and assistant messages differently.
    Assistant messages may contain suggestions, assumptions,
    or incorrect claims. They are not proof that an action occurred.

12. A troubleshooting step is confirmed only if:
    - the ticket's attempted list contains it, OR
    - a customer message explicitly states they performed it.

13. Never infer completion from an assistant message such as
    "I'm sorry that X didn't work" or "Since X didn't resolve it"
    unless the customer independently confirmed X.

14. If the assistant previously claimed an action was attempted
    but no customer confirmation or ticket record supports it,
    classify that action as unconfirmed.
15. Do not invent product-specific troubleshooting instructions,
    button combinations, reset durations, or indicator-light patterns.
    Only provide exact instructions when supported by verified
    product documentation available in the retrieved context.
    Otherwise, explain that the exact procedure needs verification
    and offer general, safe troubleshooting steps.
"""

    text = generate_with_retry(prompt)
    return text

# ---------- USER INTERFACE ----------

st.title("🧠 MemoryDesk AI")
st.caption("Customer support that remembers.")

with st.sidebar:
    st.header("Customer")
    customer_id = st.text_input(
        "Customer ID",
        value="C001"
    ).strip()

    st.divider()
    st.subheader("Conversation")
    if st.button("🆕 Start new conversation"):
        st.session_state.messages = []
        st.rerun()

    st.caption(
        "Starting a new conversation clears the visible chat, "
        "not the memory stored in Neo4j."
    )

if not customer_id:
    st.warning("Enter a customer ID to continue.")
    st.stop()

if "messages" not in st.session_state:
    st.session_state.messages = []

report_tab, chat_tab, memory_tab = st.tabs([
    "📝 Report an issue",
    "💬 Chat with agent",
    "🗂️ Retrieved memory"
])

with report_tab:
    st.subheader("Create a support ticket")

    with st.form("issue_form"):
        customer_name = st.text_input(
            "Customer name", value="Rahul"
        )
        product = st.text_input(
            "Product", value="NeoBuds Pro"
        )
        description = st.text_area(
            "Describe the issue",
            placeholder="My earbuds won't connect to my laptop..."
        )
        submitted = st.form_submit_button("Save issue")

    if submitted:
        if not product.strip() or not description.strip():
            st.error("Enter both a product and an issue.")
        else:
            ticket_id = report_issue(
                customer_id,
                customer_name.strip() or "Customer",
                product.strip(),
                description.strip()
            )
            st.success(f"Ticket {ticket_id} saved to Neo4j!")

with chat_tab:
    st.subheader("Customer support")

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    user_message = st.chat_input(
        "Describe your issue or ask a follow-up..."
    )

    
    if user_message:
        try:
            with st.spinner("Retrieving memory and thinking..."):
                
                # 1. Extract actions explicitly reported as completed.
                attempts = extract_confirmed_attempts(user_message)

                # 2. Save those attempts to the customer's open ticket.
                for attempt in attempts:
                    record_attempt(customer_id, attempt)

                # 3. Retrieve updated history so Gemini sees the new attempts.
                tickets, interactions = retrieve_history(customer_id)

                # 4. Generate the contextual reply.
                reply = generate_reply(
                    user_message, tickets, interactions
                )


                # Save both messages to Neo4j.
                save_interaction(
                    customer_id, "user", user_message
                )
                save_interaction(
                    customer_id, "assistant", reply
                )

            # Update the visible chat only after success.
            st.session_state.messages.append({
                "role": "user",
                "content": user_message
            })
            st.session_state.messages.append({
                "role": "assistant",
                "content": reply
            })

            st.rerun()

        except Exception as e:
            st.error("The agent encountered an error.")
            st.code(f"{type(e).__name__}: {e}")
            st.exception(e)
        st.rerun()

with memory_tab:
    st.subheader("Memory retrieved from Neo4j")

    tickets, interactions = retrieve_history(customer_id)

    st.metric("Previous tickets", len(tickets))
    st.metric("Saved interactions", len(interactions))

    if tickets:
        for t in tickets:
            with st.expander(
                f"{t['ticket']} · {t['product']} · {t['status']}"
            ):
                st.write("**Issue:**", t["issue"])
                st.write("**Attempts:**", t["attempted"])
                st.write("**Resolution status:**", t["resolution_status"])
                st.write("**Next step:**", t["next_step"])
    else:
        st.info("No tickets found for this customer.")

    if interactions:
        st.write("**Recent saved interactions**")
        for m in interactions:
            st.write(f"**{m['role'].title()}:** {m['content']}")
