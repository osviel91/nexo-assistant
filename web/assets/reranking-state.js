export function eligibleRerankerModels(providers, providerId) {
  const provider = (providers || []).find((item) => item.id === providerId);
  return (provider?.models || []).filter((model) => (model.capabilities || []).includes('reranking'));
}

export function rerankerState(providers, providerId, modelId, enabled) {
  const eligibleProviders = (providers || []).filter((provider) => eligibleRerankerModels(providers, provider.id).length);
  const models = eligibleRerankerModels(providers, providerId);
  const configured = models.some((model) => model.id === modelId);
  return {
    capabilityAvailable: eligibleProviders.length > 0,
    configured,
    enabled: Boolean(enabled && configured),
    providerDisabled: eligibleProviders.length === 0,
    modelDisabled: models.length === 0,
    eligibleProviders,
    models,
  };
}
