import asyncio
import json
from importlib.resources import files

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ImageContent, TextContent, Tool

from .runner import PhoneRunner
from .schemas import CancelRequest, ResumeRequest, RunRequest, WaitRequest


async def serve():
    runner = PhoneRunner()
    server = Server("phone-use-runner", instructions=files("phone_use_runner").joinpath("agent.md").read_text())
    definitions = [
        ("phone_devices", "Discover paired phones, capabilities, and launchable apps before planning.", None),
        ("phone_run", "Run a planned phone task in the background. Jev handles bounded UI decisions internally.", RunRequest),
        ("phone_wait", "Wait up to 20 seconds for completion or planning context. Images are attached only at handoff.", WaitRequest),
        ("phone_resume", "Continue a handoff with a plan update, prepared input, visual action, verification, or source-backed record_review.", ResumeRequest),
        ("phone_cancel", "Stop future actions. Already dispatched phone input may finish; retain its request ID.", CancelRequest),
    ]

    @server.list_tools()
    async def list_tools():
        return [Tool(name=name, description=description,
                     inputSchema=schema.model_json_schema() if schema else
                     {"type": "object", "properties": {}, "additionalProperties": False})
                for name, description, schema in definitions]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        shot = None
        try:
            if name == "phone_devices":
                if arguments:
                    raise ValueError("phone_devices has no arguments")
                result = await asyncio.to_thread(runner.devices)
            elif name == "phone_run":
                result = await asyncio.to_thread(runner.run, RunRequest.model_validate(arguments))
            elif name == "phone_wait":
                request = WaitRequest.model_validate(arguments)
                result = await asyncio.to_thread(runner.wait, request.task_id, request.timeout_seconds)
                shot = runner.screenshot(request.task_id)
            elif name == "phone_resume":
                result = await asyncio.to_thread(runner.resume, ResumeRequest.model_validate(arguments))
            elif name == "phone_cancel":
                request = CancelRequest.model_validate(arguments)
                result = await asyncio.to_thread(runner.cancel, request.task_id)
            else:
                raise ValueError("Unknown tool")
            content = [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            if shot and shot.get("data_base64"):
                content.append(ImageContent(type="image", mimeType=shot.get("mime_type", "image/jpeg"),
                                            data=shot["data_base64"]))
            return CallToolResult(content=content)
        except ValueError as error:
            return CallToolResult(isError=True, content=[TextContent(type="text", text=json.dumps(
                {"error": "INVALID_REQUEST", "message": str(error)}))])
        except Exception as error:
            return CallToolResult(isError=True, content=[TextContent(type="text", text=json.dumps(
                {"error": "TOOL_FAILED", "error_type": type(error).__name__}))])

    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await asyncio.to_thread(runner.close)


def main():
    asyncio.run(serve())


if __name__ == "__main__":
    main()
