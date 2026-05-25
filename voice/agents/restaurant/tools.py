try:
    from google.genai import types
except ImportError:
    raise ImportError("pip install google-genai")

TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="menu",
                description=(
                    "Fetch the latest restaurant menu with item names and prices. "
                    "You have NO prior knowledge of the menu — this tool is the ONLY "
                    "source of truth. You MUST call this tool BEFORE mentioning any "
                    "item name, price, or category to the customer. "
                    "Invocation: whenever the customer asks about the menu OR mentions "
                    "any food item. Do NOT skip this step."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={},
                    required=[],
                ),
            ),
            types.FunctionDeclaration(
                name="place_order",
                description=(
                    "Place a delivery or pickup order after the customer has confirmed all details. "
                    "Never call without explicit confirmation from the customer. "
                    "Send all data in English. For pickup orders, set order_type to 'pickup' "
                    "and address to 'BlenSpark Cafe (Pickup)'."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "customer_name": types.Schema(
                            type=types.Type.STRING,
                            description="Full name of the customer.",
                        ),
                        "phone_number": types.Schema(
                            type=types.Type.STRING,
                            description="Phone number of the customer.",
                        ),
                        "order_type": types.Schema(
                            type=types.Type.STRING,
                            description="Order type: 'delivery' or 'pickup'.",
                        ),
                        "address": types.Schema(
                            type=types.Type.STRING,
                            description="Complete delivery address, or 'BlenSpark Cafe (Pickup)' for pickup orders.",
                        ),
                        "landmark": types.Schema(
                            type=types.Type.STRING,
                            description="Nearby landmark for delivery. Empty string for pickup.",
                        ),
                        "items": types.Schema(
                            type=types.Type.ARRAY,
                            description="List of ordered items with name, qty, and price.",
                            items=types.Schema(
                                type=types.Type.OBJECT,
                                properties={
                                    "name":  types.Schema(type=types.Type.STRING,  description="Item name."),
                                    "qty":   types.Schema(type=types.Type.INTEGER, description="Quantity ordered."),
                                    "price": types.Schema(type=types.Type.INTEGER, description="Unit price of the item."),
                                },
                                required=["name", "qty", "price"],
                            ),
                        ),
                        "total_price": types.Schema(
                            type=types.Type.INTEGER,
                            description="Total price of the order.",
                        ),
                    },
                    required=["customer_name", "phone_number", "order_type", "address", "items", "total_price"],
                ),
            ),
        ]
    )
]
