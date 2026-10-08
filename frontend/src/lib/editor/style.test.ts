/**
 * The editor must reserve and draw the same picture height the layout engine
 * reserved - a diagram drawn smaller than its slot leaves the difference as a
 * blank band, a failed picture should take almost nothing.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { MAX_DIAGRAM_IMAGE_HEIGHT, MAX_IMAGE_HEIGHT, MISSING_IMAGE_HEIGHT, imageBoxHeight } from "./style.ts";

const image = (content: Record<string, unknown>, width = 666) =>
  ({
    id: "b",
    type: "image",
    content,
    style: {},
    layout: { x: 0, y: 0, width, height: 0, z_index: 0 },
    meta: {},
  }) as never;

test("a decorative picture is capped at the picture-sized box", () => {
  assert.equal(imageBoxHeight(image({ path: "a.jpg", width: 1024, height: 1024 })), MAX_IMAGE_HEIGHT);
});

test("a tall diagram keeps its readable height instead of the thumbnail cap", () => {
  const height = imageBoxHeight(image({ kind: "diagram", path: "a.svg", width: 880, height: 1402 }));
  assert.equal(height, MAX_DIAGRAM_IMAGE_HEIGHT);
  assert.ok(height > MAX_IMAGE_HEIGHT);
});

test("a short diagram follows its own aspect ratio", () => {
  const height = imageBoxHeight(image({ kind: "diagram", path: "a.svg", width: 880, height: 233 }));
  assert.ok(Math.abs(height - (666 * 233) / 880) < 0.01);
});

test("a failed picture is a one-line strip, not the picture's slot", () => {
  assert.equal(imageBoxHeight(image({ error: "429", width: 1024, height: 1024 })), MISSING_IMAGE_HEIGHT);
});

test("a picture not generated yet keeps its slot", () => {
  assert.ok(imageBoxHeight(image({})) > MISSING_IMAGE_HEIGHT);
});
