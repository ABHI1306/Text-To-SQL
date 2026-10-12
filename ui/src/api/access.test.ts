import { describe, expect, it } from 'vitest'

import { hasCapability } from './access'

describe('hasCapability', () => {
  it('allows a capability the account holds', () => {
    expect(hasCapability(['ask_question', 'view_shared'], 'ask_question')).toBe(true)
  })

  it('denies a capability the account does not hold', () => {
    expect(hasCapability(['ask_question'], 'manage_users')).toBe(false)
  })
})
