"""Prompts for the Visual Decomposition Engine.

This stage is intentionally separate from reconstruction. It does not decide the
medical strategy; it decomposes the source infographic into reusable visual
components: useful visuals, labels, backgrounds, watermarks/UI and decorative
layers. The output is a component map that the Python crop/assemble engine can
execute.
"""

VISUAL_DECOMPOSITION_SYSTEM_PROMPT = """
You are a senior visual decomposition engine for medical infographics.

Your task is NOT to rewrite the infographic and NOT to generate new content.
Your task is to inspect the source image and break it into semantic/design
components that can later be extracted by code.

Think in layers:
1. global background;
2. repeated unit/card containers;
3. useful visual objects/photos/icons;
4. text labels/captions;
5. decorative elements;
6. watermark, username, social UI;
7. elements that must be replaced or generated new.

For each final unit provided by the program, find the relevant components in the
source image. Do not crop entire cards if only a useful visual is needed.
Separate useful visuals from text and backgrounds.

CRITICAL MEDICAL SEMANTIC RULE:
For medical comparison infographics, every card must be decomposed into semantic
roles:
- PRIMARY MEDICAL OBJECT: bite mark, skin reaction, lesion, rash, wound,
  inflammation, symptom photo. This is the main evidence and must never be lost.
- SECONDARY CONTEXT OBJECT: insect, arthropod, icon, tool, arrow or other
  object that explains the cause/context.
- REMOVE/RECREATE: text labels, captions, old background, watermarks, username,
  social UI, old branding.

If an insect overlaps a bite/reaction, do NOT merge them into one crop. Return
separate components: one PRIMARY component for the bite/reaction and one
SECONDARY component for the insect/arthropod. The bite/reaction always survives,
even if the insect is larger or more visually salient.

Return strict JSON only. No markdown, no comments.
""".strip()

VISUAL_DECOMPOSITION_USER_TEMPLATE = """
Analyze the attached infographic and build a component map for extraction.

Final units/cards that the program wants to build:
{cards_json}

Reconstruction contract and replacement rules:
{contract_summary}

Rules:
- For each unit, identify useful visual components separately.
- Typical component types: bite_photo, symptom_photo, object_photo, insect_icon,
  medical_icon, product_photo, text_label, background, decorative, watermark,
  social_ui.
- For each useful visual component, provide tight normalized bbox coordinates
  relative to the source image: x, y, w, h, values 0..1.
- For every medical card, explicitly identify primary_medical_object and
  secondary_context_object. The PRIMARY object is the bite/skin reaction/lesion.
  The SECONDARY object is the insect/arthropod/context icon.
- The primary medical object must be isolated independently from the insect,
  even when they overlap. If needed, use two bboxes.
- If the useful visual has multiple parts, return multiple components. Example:
  one PRIMARY component for the bite mark/skin reaction and one SECONDARY
  component for the insect.
- If a text label is visible, mark it action="remove". Text will be recreated.
- If a background/card color is visible, mark it action="remove" unless it is
  medically/visually essential.
- If an element is forbidden/replaced by contract, do NOT preserve it. Use
  action="replace" or "generate_new".
- If the source element cannot be safely extracted, use action="generate_new".
- Do not include source social interface, likes, username, watermark, or old
  branding.
- Each bbox must be tight around the actual visual component, not the whole card.
- Do not use component type "visual_cluster", "whole_card" or "card_image" for
  medical comparison items. Break the unit into primary and secondary components.
- For circle/oval bite photos, bbox must cover the entire visible circle/skin
  reaction area, not just the center spot.
- If a unit has a boundary such as circle/square/rectangle, note boundary_type.

Required JSON shape:
{
  "infographic_structure": {
    "layout": "...",
    "repeating_unit_type": "...",
    "global_design_layers": ["..."]
  },
  "units": [
    {
      "unit_id": "...",
      "title": "...",
      "source_title": "...",
      "unit_role": "comparison_item | warning | action | footer | other",
      "extraction_strategy": "preserve_components | generate_new | replace_with_new",
      "components": [
        {
          "component_id": "...",
          "type": "bite_photo | bite_area | skin_reaction | lesion_area | insect_icon | arthropod_icon | text_label | background | watermark | social_ui | other",
          "semantic_role": "primary_medical_object | secondary_context_object | remove_recreate | decorative",
          "priority": "primary | secondary | remove",
          "action": "preserve | remove | recreate | replace | generate_new",
          "boundary_type": "circle | soft-circle | square | rectangle | skin-patch | lesion-area | irregular-cluster | none | unknown",
          "bbox": {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0},
          "keep": ["..."],
          "remove": ["..."],
          "reason": "..."
        }
      ],
      "notes": "..."
    }
  ],
  "quality_notes": ["..."]
}
""".strip()
