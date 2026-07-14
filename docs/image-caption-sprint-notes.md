# Image And Caption Sampling Notes

Source used for sampling:

- `input/פסיכולוגיה התפתחותית כרך א.pdf`

Sample pages rendered:

- `51, 94, 142, 159, 166, 197, 223, 246, 252, 291, 308, 420`
- Non-standard follow-up sample: `21, 31, 41, 44, 50`

Generated local artifacts:

- `generated-readers/image-caption-sample-previews/`
- `generated-readers/image-caption-sample-summary.json`

## Observed Layout Patterns

### One Image, Local Caption

Common case. The image has a nearby caption below or beside it.

Examples:

- Page 166
- Page 223
- Page 308
- Page 31: caption is beside the image, vertically centered rather than below it.
- Page 41: caption is to the right and near the bottom of a large image, not horizontally adjacent.
- Page 50: caption is left/below a right-side image and competes with nearby body text.

Recommended handling:

- Detect content image bounding boxes.
- Find nearby caption blocks.
- Prefer captions with horizontal overlap or a close edge-to-edge distance.

### Multiple Independent Images On One Page

Several images each have their own caption. Page 51 is the cleanest example.

Recommended handling:

- Pair each image with the nearest compatible caption.
- Use horizontal overlap as a strong signal.
- Avoid assigning one caption to multiple images unless the caption spans the whole group.

### Image Gallery With Local Labels And One Shared Caption

Some figures contain many small images. Each image has a short local label, while the full figure caption is below the group.

Examples:

- Page 246
- Page 420

Recommended handling:

- Cluster nearby images into a figure group when their boxes share a visual background or tight bounding rectangle.
- Treat very short nearby labels as image labels, not full captions.
- Attach the long figure caption to the group, not to every individual image.

### Multi-Part Figure With Shared Caption

Several sub-images appear as parts of one figure and share one caption.

Examples:

- Page 252
- Page 291

Recommended handling:

- Cluster images by vertical band and page region.
- If the nearest caption is far from some sub-images but close to the group bounds, attach it to the group.

### Non-Caption Side Notes Near Images

Some small side text blocks are close to images but are not image captions.

Examples:

- Page 44: a narrow side note appears above/right of a large photo. It is visually closer than any real image caption, but it is explanatory side text, not a caption.
- Page 21: the page is a meta/instructional layout with screenshots and floating callout boxes. Treating every small text block as a caption would be wrong.

Recommended handling:

- Match images only against blocks classified as likely captions, not arbitrary nearby body or side-note text.
- Add a caption confidence score that considers text style, width, distance, overlap, and whether the block looks like a term definition or sidebar.
- Keep low-confidence matches in debug output only until reviewed.

## Updated Matching Implications

The non-standard sample changes the matching model:

- Caption location cannot be assumed to be below the image.
- A valid caption can be beside the image with vertical overlap.
- A valid caption can be separated horizontally from a large image if it occupies the nearby free column.
- The nearest small block is not always a caption.
- Screenshot/callout pages should probably become grouped figure regions, not individual reader images.

## Proposed Next Implementation Step

Add a debug-only image region pass before embedding images in the reader:

1. Detect content images and ignore full-page background/mask images.
2. Add image regions to `classification.regions`.
3. Add tentative nearest-caption matches to the layout debug JSON.
4. Group dense image clusters before assigning long captions.
5. Include match confidence and reason fields such as `below_overlap`, `side_overlap`, `group_caption`, or `low_confidence_sidebar`.
6. Keep the generated HTML unchanged until the debug output looks stable on sampled pages.

This keeps the next sprint low-risk: first expose image geometry and matching decisions, then render images in the reader.
