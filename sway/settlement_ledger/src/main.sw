contract;

/// Immutable audit ledger for proven settlements.
///
/// The beneficiary is a compile-time constant, so no caller can redirect a
/// settlement. `commitment` is the Poseidon-style digest the off-chain prover
/// derived; recording it once makes replay impossible because the map entry
/// already exists.
configurable {
    LOCKED_BENEFICIARY: b256 = 0x000000000000000000000000e3d1c7dcd5e95b6441716b65f9c46b73e91b4d47,
}

storage {
    settled: StorageMap<b256, bool> = StorageMap {},
    delivered_total: StorageMap<b256, u64> = StorageMap {},
    settlement_count: u64 = 0,
    grand_total: u64 = 0,
}

abi SettlementLedger {
    /// Record a settlement the prover has already certified.
    #[storage(read, write)]
    fn record(beneficiary: b256, delta: u64, net_profit: u64, commitment: b256) -> bool;

    #[storage(read)]
    fn is_settled(commitment: b256) -> bool;

    #[storage(read)]
    fn delivered_to(beneficiary: b256) -> u64;

    #[storage(read)]
    fn totals() -> (u64, u64);

    fn locked_beneficiary() -> b256;
}

impl SettlementLedger for Contract {
    #[storage(read, write)]
    fn record(beneficiary: b256, delta: u64, net_profit: u64, commitment: b256) -> bool {
        require(beneficiary == LOCKED_BENEFICIARY, "beneficiary is locked");
        require(!storage.settled.get(commitment).try_read().unwrap_or(false), "commitment already settled");
        require(net_profit > 0, "net profit must be positive");
        require(delta >= net_profit, "delta must cover the net profit");

        storage.settled.insert(commitment, true);
        let prior = storage.delivered_total.get(beneficiary).try_read().unwrap_or(0);
        storage.delivered_total.insert(beneficiary, prior + delta);
        storage.settlement_count.write(storage.settlement_count.read() + 1);
        storage.grand_total.write(storage.grand_total.read() + delta);
        true
    }

    #[storage(read)]
    fn is_settled(commitment: b256) -> bool {
        storage.settled.get(commitment).try_read().unwrap_or(false)
    }

    #[storage(read)]
    fn delivered_to(beneficiary: b256) -> u64 {
        storage.delivered_total.get(beneficiary).try_read().unwrap_or(0)
    }

    #[storage(read)]
    fn totals() -> (u64, u64) {
        (storage.settlement_count.read(), storage.grand_total.read())
    }

    fn locked_beneficiary() -> b256 {
        LOCKED_BENEFICIARY
    }
}
