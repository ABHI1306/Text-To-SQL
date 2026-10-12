export function hasCapability(
  capabilities: readonly string[],
  requiredCapability: string,
): boolean {
  return capabilities.includes(requiredCapability)
}
