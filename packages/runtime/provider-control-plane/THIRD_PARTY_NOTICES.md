# Provider Control Plane third-party notices

This Zyra package contains modified, productized mechanisms from the following
sources. The listed repositories are provenance only and are not runtime
dependencies.

## OpenCode

- Source revision: `adf178a6b95c61506ddaadaf4dd062badb4a8fda`
- Source files: provider catalog, integration, credential, AISDK, model, auth,
  and configuration mechanisms recorded in the M1-S05D-01 source ledger.
- License: MIT.
- Copyright: 2025 opencode.

## oh-my-pi

- Source revision: `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca`
- Source files: OpenAI Chat, OpenAI Responses, Anthropic Messages, simple
  Responses, and auth-retry mechanisms recorded in the M1-S05D-01 source
  ledger.
- License: MIT.
- Copyright: 2025 Mario Zechner; 2025-2026 Can Bölük.
- The hand-maintained OpenAI Responses wire types record derivation from
  `openai-node` v6.42.0, licensed under Apache-2.0.

The direct ports were modified to use Zyra catalog revisions, credential
references, immutable route leases, attempt records, byte accounting, error
taxonomy, process supervision, and API/task-graph boundaries.

## MIT license text

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Apache-2.0 attribution for the OpenAI wire contract is preserved in the
source header and in this notice. The full license terms are available from
the Apache Software Foundation at `https://www.apache.org/licenses/LICENSE-2.0`.
