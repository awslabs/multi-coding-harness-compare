# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Keep the forced tool on structured-output repair, without reasoning options."""
from strands.models import BedrockModel


class ThinkingPlanningModel(BedrockModel):
    def format_request(self, messages, tool_specs=None, system_prompt_content=None,
                       tool_choice=None, dynamic_trailing_blocks=0, **kwargs):
        request = super().format_request(
            messages, tool_specs, system_prompt_content, tool_choice,
            dynamic_trailing_blocks, **kwargs)
        # Strands forces the output tool on its repair turn. Relaxing that to
        # auto selection lets the model answer in prose, or submit filler for
        # the fields the schema requires, so keep the forced tool and drop the
        # reasoning options instead: the reasoning that earns the result already
        # happened on turn one. Verified live against us.anthropic.claude-opus-5
        # that a forced repair turn is accepted with reasoning blocks still in
        # history, both with and without these fields.
        if tool_choice and ("any" in tool_choice or "tool" in tool_choice):
            request.pop("additionalModelRequestFields", None)
        return request
