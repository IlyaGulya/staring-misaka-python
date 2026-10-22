const commandPattern =
  /^\/(approve|unapprove|toggle_bot|queue_status|explain|action|start)(?:@[A-Za-z0-9_]+)?(?:\s|$)/i;

/** Commands remain evidence, but are marked consistently by both transports. */
export function isCommandText(text: string): boolean {
  return commandPattern.test(text);
}
