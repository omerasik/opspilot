import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, ConfigDict, Field


class Vendor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    vendor_id: str
    name: str = Field(min_length=1, max_length=100)
    service: str = Field(min_length=1, max_length=100)
    approved: bool


class ToolFault(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class CatalogClient:
    allowed = frozenset({"lookup_vendor"})

    async def call(self, name: str, vendor_id: str) -> Vendor:
        if name not in self.allowed:
            raise ToolFault("tool_not_allowed")
        if not 1 <= len(vendor_id) <= 40 or any(character not in "abcdefghijklmnopqrstuvwxyz-0123456789" for character in vendor_id):
            raise ToolFault("invalid_vendor_id")
        server = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).with_name("catalog.py"))],
            env={"PYTHONUTF8": "1"},
        )
        try:
            async with asyncio.timeout(12):
                async with stdio_client(server) as (incoming, outgoing):
                    async with ClientSession(incoming, outgoing) as session:
                        await session.initialize()
                        offered = await session.list_tools()
                        if name not in {item.name for item in offered.tools}:
                            raise ToolFault("tool_missing")
                        response = await session.call_tool(name, {"vendor_id": vendor_id})
        except ToolFault:
            raise
        except Exception as error:
            raise ToolFault("catalog_unavailable", retryable=True) from error
        if response.is_error:
            raise ToolFault("catalog_error")
        try:
            if response.structured_content is not None:
                vendor = Vendor.model_validate(response.structured_content)
            else:
                vendor = Vendor.model_validate_json(response.content[0].text)
        except (ValueError, AttributeError, IndexError) as error:
            raise ToolFault("vendor_not_found") from error
        if vendor.vendor_id != vendor_id:
            raise ToolFault("catalog_result_mismatch")
        return vendor
