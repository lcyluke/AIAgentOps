#!/usr/bin/env node
/**
 * APEX Operations MCP Server
 *
 * Exposes 6 tools for managing APEX multi-agent sessions from within
 * Kiro, Cursor, or any MCP-compatible client via stdio transport.
 *
 * Talks to apexd via Unix socket (~/.apex/apexd.sock).
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";

// ── Socket client to apexd ──────────────────────────────────

const SOCKET_PATH = path.join(os.homedir(), ".apex", "apexd.sock");

function sendCommand(command: string, payload: Record<string, unknown> = {}): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const client = net.createConnection(SOCKET_PATH, () => {
      const msg = JSON.stringify({ command, ...payload }) + "\n";
      client.write(msg);
    });

    let data = "";
    client.on("data", (chunk) => {
      data += chunk.toString();
      if (data.includes("\n")) {
        client.end();
        try {
          resolve(JSON.parse(data.trim()));
        } catch {
          resolve({ ok: false, error: "invalid response", raw: data });
        }
      }
    });

    client.on("error", (err) => {
      resolve({ ok: false, error: `socket error: ${err.message}` });
    });

    setTimeout(() => {
      client.destroy();
      resolve({ ok: false, error: "timeout" });
    }, 5000);
  });
}

// ── Tool definitions ────────────────────────────────────────

const TOOLS = [
  {
    name: "apex_spawn",
    description: "Spawn a new agent session to execute a task",
    inputSchema: {
      type: "object",
      properties: {
        agent: { type: "string", description: "Agent name (e.g. architect, ops-engineer)" },
        task: { type: "string", description: "Task description" },
        cwd: { type: "string", description: "Working directory" },
      },
      required: ["agent", "task"],
    },
  },
  {
    name: "apex_status",
    description: "Query session status or list all active sessions",
    inputSchema: {
      type: "object",
      properties: {
        session_id: { type: "string", description: "Optional session ID; omit to list all" },
      },
    },
  },
  {
    name: "apex_blackboard",
    description: "Search the shared blackboard for cross-agent knowledge",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "Search query (text match)" },
      },
      required: ["query"],
    },
  },
  {
    name: "apex_claims",
    description: "List active task claims to avoid duplicate work",
    inputSchema: {
      type: "object",
      properties: {},
    },
  },
  {
    name: "apex_stop",
    description: "Stop a running agent session",
    inputSchema: {
      type: "object",
      properties: {
        session_id: { type: "string", description: "Session ID to stop" },
      },
      required: ["session_id"],
    },
  },
  {
    name: "apex_doctor",
    description: "Run health checks on the APEX system",
    inputSchema: {
      type: "object",
      properties: {},
    },
  },
];

// ── Server ──────────────────────────────────────────────────

const server = new Server(
  { name: "apex-ops-mcp", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  switch (name) {
    case "apex_spawn":
      return {
        content: [{ type: "text", text: JSON.stringify(
          await sendCommand("spawn", { agent: args?.agent, task: args?.task, cwd: args?.cwd }),
          null, 2
        ) }],
      };

    case "apex_status":
      return {
        content: [{ type: "text", text: JSON.stringify(
          await sendCommand("status", args?.session_id ? { session_id: args.session_id } : {}),
          null, 2
        ) }],
      };

    case "apex_blackboard":
      return {
        content: [{ type: "text", text: JSON.stringify(
          await sendCommand("blackboard", { query: args?.query }),
          null, 2
        ) }],
      };

    case "apex_claims":
      return {
        content: [{ type: "text", text: JSON.stringify(
          await sendCommand("claims", {}),
          null, 2
        ) }],
      };

    case "apex_stop":
      return {
        content: [{ type: "text", text: JSON.stringify(
          await sendCommand("stop", { session_id: args?.session_id }),
          null, 2
        ) }],
      };

    case "apex_doctor":
      return {
        content: [{ type: "text", text: JSON.stringify(
          await sendCommand("doctor", {}),
          null, 2
        ) }],
      };

    default:
      return { content: [{ type: "text", text: `Unknown tool: ${name}` }], isError: true };
  }
});

// ── Start ───────────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
