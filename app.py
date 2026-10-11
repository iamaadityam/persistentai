
import os
from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()


def require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


URI = require_env("NEO4J_URI")
USERNAME = require_env("NEO4J_USERNAME")
PASSWORD = require_env("NEO4J_PASSWORD")

driver = GraphDatabase.driver(
    URI,
    auth=(USERNAME, PASSWORD),
)

def create_support_ticket(tx):
    tx.run("""
        MERGE (c:Customer {id: $customer_id})
        SET c.name = $customer_name

        MERGE (p:Product {id: $product_id})
        SET p.name = $product_name

        MERGE (i:Issue {id: $issue_id})
        SET i.description = $issue_description,
            i.type = $issue_type

        MERGE (t:SupportTicket {id: $ticket_id})
        SET t.status = 'open',
            t.attempted = $attempted

        MERGE (r:Resolution {id: $resolution_id})
        SET r.status = 'unresolved',
            r.next_step = $next_step

        MERGE (c)-[:OWNS]->(p)
        MERGE (c)-[:REPORTED]->(i)
        MERGE (i)-[:TRACKED_IN]->(t)
        MERGE (t)-[:HAS_RESOLUTION]->(r)
    """,
        customer_id="C001",
        customer_name="Rahul",
        product_id="P001",
        product_name="NeoBuds Pro",
        issue_id="I001",
        issue_description="Earbuds won't connect to laptop",
        issue_type="Bluetooth connectivity",
        ticket_id="TICKET-001",
        attempted=[
            "Restarted earbuds",
            "Reset laptop Bluetooth"
        ],
        resolution_id="R001",
        next_step="Check earbuds firmware"
    )


def retrieve_history(tx):
    result = tx.run("""
        MATCH (c:Customer {id: $customer_id})
              -[:REPORTED]->(i:Issue)
              -[:TRACKED_IN]->(t:SupportTicket)
              -[:HAS_RESOLUTION]->(r:Resolution)
        OPTIONAL MATCH (c)-[:OWNS]->(p:Product)
        RETURN c.name AS customer,
               p.name AS product,
               i.description AS issue,
               t.id AS ticket,
               t.status AS status,
               t.attempted AS attempted,
               r.status AS resolution_status,
               r.next_step AS next_step
    """, customer_id="C001")

    return [record.data() for record in result]


try:
    driver.verify_connectivity()

    with driver.session() as session:
        session.execute_write(create_support_ticket)
        history = session.execute_read(retrieve_history)

        for ticket in history:
            print("\n--- Retrieved Support History ---")
            for key, value in ticket.items():
                print(f"{key}: {value}")

    print("\n✅ Support graph created and retrieved!")

finally:
    driver.close()
