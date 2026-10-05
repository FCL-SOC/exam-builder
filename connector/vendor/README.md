# Vendored

`mcp-apps-app-1.7.5.js` is `dist/src/app-with-deps.js` from
[@modelcontextprotocol/ext-apps](https://github.com/modelcontextprotocol/ext-apps) 1.7.5, unchanged (licence in
`mcp-apps-LICENSE`). It is the official client the lesson plan preview uses to talk to Claude. It is embedded in
the preview page itself, so nothing is fetched from the internet, which the school's web filter could block.

To update it: `npm pack @modelcontextprotocol/ext-apps@<version>`, copy `package/dist/src/app-with-deps.js` here
under the new version's name, and change `MCP_APPS_CLIENT` in `connector/server.py`.
