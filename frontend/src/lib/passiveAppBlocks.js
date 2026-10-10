/** Trusted pre-execution admission for passive transcript app sessions. */
export function passiveAppBlockAllowed(app) {
  return app?.capability_contract?.runtime?.['chat.blocks.passive']?.version === 1
    && typeof app?.passive_block_module_digest === 'string'
    && /^[a-f0-9]{64}$/.test(app.passive_block_module_digest)
}
