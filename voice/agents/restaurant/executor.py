import logging
import os

logger = logging.getLogger(__name__)


async def execute_tool(tool_name: str, tool_args: dict) -> dict:
    import aiohttp
    base = os.getenv("API_BASE_URL", "https://web-production-00424.up.railway.app")

    try:
        async with aiohttp.ClientSession() as http:
            if tool_name == "menu":
                async with http.get(f"{base}/menu/") as resp:
                    resp.raise_for_status()
                    return await resp.json()

            elif tool_name == "place_order":
                payload = {
                    "customer_name": tool_args.get("customer_name", ""),
                    "phone_number":  tool_args.get("phone_number", ""),
                    "order_type":    tool_args.get("order_type", "delivery"),
                    "address":       tool_args.get("address", ""),
                    "landmark":      tool_args.get("landmark", ""),
                    "items":         tool_args.get("items", []),
                    "total_price":   tool_args.get("total_price", 0),
                }
                async with http.post(f"{base}/orders/", json=payload) as resp:
                    resp.raise_for_status()
                    return await resp.json()

            else:
                return {"error": f"Unknown tool: {tool_name}"}

    except Exception as e:
        return {"error": str(e)}
