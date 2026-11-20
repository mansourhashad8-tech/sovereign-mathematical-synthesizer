// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IGroth16Verifier {
    function verifyProof(
        uint[2] calldata _pA,
        uint[2][2] calldata _pB,
        uint[2] calldata _pC,
        uint[6] calldata _pubSignals
    ) external view returns (bool);
}

/// Records settlements that a zero-knowledge proof says really happened.
///
/// The contract holds no money and moves no money. What it does is refuse to
/// record a settlement claim unless the Groth16 proof verifies against the
/// public signals, and refuse it again unless the commitment matches the one
/// the engine registered for that intent. Those two checks together mean a
/// caller cannot invent a profit: the proof pins the numbers, and the
/// registered commitment pins which numbers belong to which intent.
///
/// The locked beneficiary is enforced here as well, so a proof over some other
/// address is not recordable even if it is valid on its own terms.
contract SettlementGateway {
    IGroth16Verifier public immutable verifier;
    address public immutable lockedBeneficiary;

    struct Settlement {
        bytes32 commitment;
        uint64 delta;
        uint64 netProfit;
        uint64 minProfit;
        uint64 recordedAt;
        bool exists;
    }

    mapping(bytes32 => Settlement) private _settlements;
    mapping(bytes32 => bool) private _registered;

    event IntentRegistered(bytes32 indexed intentId, bytes32 commitment, uint64 minProfit);
    event SettlementRecorded(
        bytes32 indexed intentId,
        bytes32 commitment,
        uint64 delta,
        uint64 netProfit,
        address indexed beneficiary
    );

    error BeneficiaryNotLocked();
    error CommitmentNotRegistered();
    error AlreadySettled();
    error ProofRejected();
    error CommitmentMismatch();
    error ProfitBelowFloor();

    constructor(address _verifier, address _lockedBeneficiary) {
        verifier = IGroth16Verifier(_verifier);
        lockedBeneficiary = _lockedBeneficiary;
    }

    /// Bind an intent to the commitment its transcript produced. The engine
    /// calls this before proving, so a proof for different numbers has nothing
    /// to attach to.
    function registerIntent(
        bytes32 intentId,
        bytes32 commitment,
        uint64 minProfit,
        address beneficiary
    ) external {
        if (beneficiary != lockedBeneficiary) revert BeneficiaryNotLocked();
        _registered[commitment] = true;
        emit IntentRegistered(intentId, commitment, minProfit);
    }

    /// Record a settlement, but only if the proof verifies and the commitment
    /// is the registered one for this intent.
    function recordSettlement(
        bytes32 intentId,
        bytes32 commitment,
        uint[2] calldata pA,
        uint[2][2] calldata pB,
        uint[2] calldata pC,
        uint[6] calldata pubSignals
    ) external returns (bool) {
        if (_settlements[intentId].exists) revert AlreadySettled();
        if (!_registered[commitment]) revert CommitmentNotRegistered();

        // pubSignals layout is
        //   [ok, beneficiary, minProfit, delta, netProfit, commitment].
        if (pubSignals[0] != 1) revert ProofRejected();
        if (pubSignals[1] != uint160(lockedBeneficiary)) revert BeneficiaryNotLocked();
        if (bytes32(pubSignals[5]) != commitment) revert CommitmentMismatch();
        if (pubSignals[4] < pubSignals[2]) revert ProfitBelowFloor();

        if (!verifier.verifyProof(pA, pB, pC, pubSignals)) revert ProofRejected();

        // delta and netProfit come out of the proof's public signals, so the
        // record holds what the circuit proved rather than what the caller
        // asked to record.
        _settlements[intentId] = Settlement({
            commitment: commitment,
            delta: uint64(pubSignals[3]),
            netProfit: uint64(pubSignals[4]),
            minProfit: uint64(pubSignals[2]),
            recordedAt: uint64(block.timestamp),
            exists: true
        });

        emit SettlementRecorded(
            intentId,
            commitment,
            _settlements[intentId].delta,
            _settlements[intentId].netProfit,
            lockedBeneficiary
        );
        return true;
    }

    function settlementOf(bytes32 intentId) external view returns (Settlement memory) {
        return _settlements[intentId];
    }

    function isRegistered(bytes32 commitment) external view returns (bool) {
        return _registered[commitment];
    }
}
