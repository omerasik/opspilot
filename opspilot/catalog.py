from mcp.server.mcpserver import MCPServer

server = MCPServer("opspilot-vendor-catalog")

CATALOG = {
    "leaf-works": {"name": "Leaf Works", "approved": True, "service": "Equipment maintenance"},
    "river-studio": {"name": "River Studio", "approved": False, "service": "Design services"},
}


@server.tool()
def lookup_vendor(vendor_id: str) -> dict:
    """Look up one vendor in the local procurement catalog."""
    return {"vendor_id": vendor_id, **CATALOG.get(vendor_id, {})}


if __name__ == "__main__":
    server.run(transport="stdio")
