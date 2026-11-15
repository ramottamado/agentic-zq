"""Constants shared by the MCP server (which produces tool errors) and the agents (which read them)."""

# The MCP server puts this in the tool error when the warehouse itself cannot be reached (connection
# or Kerberos failure), as opposed to the SQL being wrong. Agents must not ask the LLM to "repair"
# SQL for such a failure: a new query cannot fix an outage.
WAREHOUSE_UNAVAILABLE_MARKER = "Warehouse unavailable"
