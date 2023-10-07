# These nodes were made using code from the Deforum extension for A1111 webui
# You can find the project here: https://github.com/deforum-art/sd-webui-deforum

from typing import List
import numexpr
import torch
import numpy as np
import pandas as pd
import re

from .ScheduleFuncs import addWeighted, check_is_number, parse_weight, prepare_prompt

def pad_conditioning_tensors_to_same_length(conditionings: List[torch.Tensor], emptystring_conditioning: torch.Tensor
                                                 ) -> List[torch.Tensor]:
        if not all([len(c.shape) in [2, 3] for c in conditionings]):
            raise ValueError("Conditioning tensors must all have either 2 dimensions (unbatched) or 3 dimensions (batched)")
         
        # ensure all conditioning tensors are 3 dimensions
        conditionings = [c.unsqueeze(0) if len(c.shape) == 2 else c for c in conditionings]
        c0_shape = conditionings[0].shape

        if not all([c.shape[0] == c0_shape[0] and c.shape[2] == c0_shape[2] for c in conditionings]):
            raise ValueError(f"All conditioning tensors must have the same batch size ({c0_shape[0]}) and number of embeddings per token ({c0_shape[1]}")
        
        if len(emptystring_conditioning.shape) == 2:
            emptystring_conditioning = emptystring_conditioning.unsqueeze(0)
        elif len(emptystring_conditioning.shape) == 1:
            emptystring_conditioning = emptystring_conditioning.unsqueeze(0).unsqueeze(0)
        empty_z = torch.cat([emptystring_conditioning] * c0_shape[0])
        max_token_count = max([c.shape[1] for c in conditionings])
        # if necessary, pad shorter tensors out with an emptystring tensor
        for i, c in enumerate(conditionings):
            while c.shape[1] < max_token_count:
                c = torch.cat([c, empty_z], dim=1)
                conditionings[i] = c
        return conditionings

def prepare_batch_prompt(prompt_series, max_frames, frame_idx, prompt_weight_1=0, prompt_weight_2=0, prompt_weight_3=0,
                         prompt_weight_4=0):  # calculate expressions from the text input and return a string
    max_f = max_frames - 1
    pattern = r'`.*?`'  # set so the expression will be read between two backticks (``)
    regex = re.compile(pattern)
    prompt_parsed = str(prompt_series)

    for match in regex.finditer(prompt_parsed):
        matched_string = match.group(0)
        parsed_string = matched_string.replace('t', f'{frame_idx}').replace("pw_a", f"prompt_weight_1").replace("pw_b",
                                                                                                                f"prompt_weight_2").replace(
            "pw_c", f"prompt_weight_3").replace("pw_d", f"prompt_weight_4").replace("max_f", f"{max_f}").replace('`',
                                                                                                                 '')  # replace t, max_f and `` respectively
        parsed_value = numexpr.evaluate(parsed_string)
        prompt_parsed = prompt_parsed.replace(matched_string, str(parsed_value))
    return prompt_parsed.strip()

def interpolate_prompt_series(animation_prompts, max_frames, pre_text, app_text, prompt_weight_1=[],
                              prompt_weight_2=[], prompt_weight_3=[],
                              prompt_weight_4=[]):  # parse the conditioning strength and determine in-betweens.
    # Get prompts sorted by keyframe
    max_f = max_frames  # needed for numexpr even though it doesn't look like it's in use.
    parsed_animation_prompts = {}
    for key, value in animation_prompts.items():
        if check_is_number(key):  # default case 0:(1 + t %5), 30:(5-t%2)
            parsed_animation_prompts[key] = value
        else:  # math on the left hand side case 0:(1 + t %5), maxKeyframes/2:(5-t%2)
            parsed_animation_prompts[int(numexpr.evaluate(key))] = value

    sorted_prompts = sorted(parsed_animation_prompts.items(), key=lambda item: int(item[0]))

    # Setup containers for interpolated prompts
    cur_prompt_series = pd.Series([np.nan for a in range(max_frames)])
    nxt_prompt_series = pd.Series([np.nan for a in range(max_frames)])

    # simple array for strength values
    weight_series = [np.nan] * max_frames

    # in case there is only one keyed promt, set all prompts to that prompt
    if len(sorted_prompts) - 1 == 0:
        for i in range(0, len(cur_prompt_series) - 1):
            current_prompt = sorted_prompts[0][1]
            cur_prompt_series[i] = str(pre_text) + " " + str(current_prompt) + " " + str(app_text)
            nxt_prompt_series[i] = str(pre_text) + " " + str(current_prompt) + " " + str(app_text)

    # Initialized outside of loop for nan check
    current_key = 0
    next_key = 0

    # For every keyframe prompt except the last
    for i in range(0, len(sorted_prompts) - 1):
        # Get current and next keyframe
        current_key = int(sorted_prompts[i][0])
        next_key = int(sorted_prompts[i + 1][0])

        # Ensure there's no weird ordering issues or duplication in the animation prompts
        # (unlikely because we sort above, and the json parser will strip dupes)
        if current_key >= next_key:
            print(
                f"WARNING: Sequential prompt keyframes {i}:{current_key} and {i + 1}:{next_key} are not monotonously increasing; skipping interpolation.")
            continue

        # Get current and next keyframes' positive and negative prompts (if any)
        current_prompt = sorted_prompts[i][1]
        next_prompt = sorted_prompts[i + 1][1]

        # Calculate how much to shift the weight from current to next prompt at each frame.
        weight_step = 1 / (next_key - current_key)

        for f in range(max(current_key, 0), min(next_key, len(cur_prompt_series))):
            next_weight = weight_step * (f - current_key)
            current_weight = 1 - next_weight

            # add the appropriate prompts and weights to their respective containers.
            # print(weight_series)
            # print(weight_series[f])
            cur_prompt_series[f] = ''
            nxt_prompt_series[f] = ''
            weight_series[f] = 0.0

            cur_prompt_series[f] += (str(pre_text) + " " + str(current_prompt) + " " + str(app_text))
            nxt_prompt_series[f] += (str(pre_text) + " " + str(next_prompt) + " " + str(app_text))

            weight_series[f] += current_weight

        current_key = next_key
        next_key = max_frames
        current_weight = 0.0
        # second loop to catch any nan runoff
        for f in range(current_key, next_key):
            next_weight = weight_step * (f - current_key)

            # add the appropriate prompts and weights to their respective containers.
            cur_prompt_series[f] = ''
            nxt_prompt_series[f] = ''
            weight_series[f] = current_weight

            cur_prompt_series[f] += (str(pre_text) + " " + str(current_prompt) + " " + str(app_text))
            nxt_prompt_series[f] += (str(pre_text) + " " + str(next_prompt) + " " + str(app_text))

    if isinstance(prompt_weight_1, int):
        prompt_weight_1 = tuple([prompt_weight_1] * max_frames)

    if isinstance(prompt_weight_2, int):
        prompt_weight_2 = tuple([prompt_weight_2] * max_frames)

    if isinstance(prompt_weight_3, int):
        prompt_weight_3 = tuple([prompt_weight_3] * max_frames)

    if isinstance(prompt_weight_4, int):
        prompt_weight_4 = tuple([prompt_weight_4] * max_frames)

    # Evaluate the current and next prompt's expressions
    for i in range(len(cur_prompt_series)):
        cur_prompt_series[i] = prepare_batch_prompt(cur_prompt_series[i], max_frames, i, prompt_weight_1[i],
                                                    prompt_weight_2[i], prompt_weight_3[i], prompt_weight_4[i])
        nxt_prompt_series[i] = prepare_batch_prompt(nxt_prompt_series[i], max_frames, i, prompt_weight_1[i],
                                                    prompt_weight_2[i], prompt_weight_3[i], prompt_weight_4[i])

    # Show the to/from prompts with evaluated expressions for transparency.
    for i in range(len(cur_prompt_series)):
        print("\n", "Max Frames: ", max_frames, "\n", "Current Prompt: ", cur_prompt_series[i], "\n",
              "Next Prompt: ", nxt_prompt_series[i], "\n", "Strength : ", weight_series[i], "\n")

    # Output methods depending if the prompts are the same or if the current frame is a keyframe.
    # if it is an in-between frame and the prompts differ, composable diffusion will be performed.
    return (cur_prompt_series, nxt_prompt_series, weight_series)


def BatchPoolAnimConditioning(cur_prompt_series, nxt_prompt_series, weight_series, clip):
    pooled_out = []
    cond_out = []
    tokens = clip.tokenize(str(''))
    cond_empty = clip.encode_from_tokens(tokens, return_pooled=False)
    interpolated_cond_empty = cond_empty[0][0]
    for i in range(len(cur_prompt_series)):
        tokens = clip.tokenize(str(cur_prompt_series[i]))
        cond_to, pooled_to = clip.encode_from_tokens(tokens, return_pooled=True)

        tokens = clip.tokenize(str(nxt_prompt_series[i]))
        cond_from, pooled_from = clip.encode_from_tokens(tokens, return_pooled=True)

        interpolated_conditioning = addWeighted([[cond_to, {"pooled_output": pooled_to}]],
                                                [[cond_from, {"pooled_output": pooled_from}]],
                                                weight_series[i])

        interpolated_cond = interpolated_conditioning[0][0]
        interpolated_pooled = interpolated_conditioning[0][1].get("pooled_output", pooled_from)

        pooled_out.append(interpolated_pooled)
        cond_out.append(interpolated_cond)

    cond_out = pad_conditioning_tensors_to_same_length(cond_out, interpolated_cond_empty)

    final_pooled_output = torch.cat(pooled_out, dim=0)
    final_conditioning = torch.cat(cond_out, dim=0)

    return [[final_conditioning, {"pooled_output": final_pooled_output}]]
