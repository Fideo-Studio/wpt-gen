# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Gap scoring dictionary

Use a shared typed dict so the scorer and adapter agree on the shape without
an undefined class.

"""

from typing import TypedDict

from wptgen.phases.report_render import RequirementAudit


class GapPayload(TypedDict):
    rows: list[RequirementAudit]
    suggestions_by_requirement: dict[str, list[str]]
    batch_paths: set[str]
    spec_quotes: list[str]
    spec_quotes_by_requirement: dict[str, str]
