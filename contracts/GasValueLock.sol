// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/**
 * @title GasValueLock
 * @notice Atomic value-gas entanglement: gas is paid to the relayer out of the
 *         settled value itself, inside one transaction. There is no window in
 *         which the value has moved but the relayer is unpaid, and no window in
 *         which the relayer is paid but the value has not moved.
 *
 * The economic point: the relayer is compensated in the *value token*, not in
 * ETH. That is what removes the "testnet has gas, mainnet has value" split —
 * an agent holding zero ETH can still settle, because its gas comes out of the
 * profit it is delivering.
 *
 * Every precondition is a hard revert. There is no partial execution and no
 * "settled but unpaid" state, because a state machine with a half-way point is
 * exactly where a payout can be lost.
 */
interface IERC20 {
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

interface IERC20Permit {
    function permit(
        address owner,
        address spender,
        uint256 value,
        uint256 deadline,
        uint8 v,
        bytes32 r,
        bytes32 s
    ) external;
}

contract GasValueLock {
    /// @notice The only address that can ever receive settled value.
    address public constant BENEFICIARY = 0xE3d1c7dCD5E95b6441716b65F9C46b73e91b4d47;

    /// @notice Replay protection: an intent id settles at most once.
    mapping(bytes32 => bool) public settled;

    /// @notice Accumulated gas reserve, denominated in the value token.
    mapping(address => uint256) public gasReserve;

    address public immutable guardian;

    error BeneficiaryLocked();
    error IntentAlreadySettled(bytes32 intentId);
    error ProofNotSatisfied();
    error RelayerNotPaid();
    error NetProfitNotPositive();
    error ValueTransferFailed();
    error ReserveTransferFailed();
    error ZeroAddress();
    error InsufficientReserve(uint256 available, uint256 required);

    event ValueGasEntangled(
        bytes32 indexed intentId,
        address indexed token,
        address indexed relayer,
        uint256 netProfit,
        uint256 relayerFee,
        uint256 toBeneficiary,
        uint256 toReserve
    );
    event ReserveToppedUp(address indexed token, uint256 amount);
    event ReserveSpent(address indexed token, address indexed relayer, uint256 amount);

    modifier onlyGuardian() {
        require(msg.sender == guardian, "not guardian");
        _;
    }

    constructor() {
        guardian = msg.sender;
    }

    /**
     * @notice Settle with an EIP-2612 permit instead of a prior approval.
     *
     * This is the entry point that makes the design actually work for a
     * zero-ETH agent. An account with no ETH cannot send even an `approve`
     * transaction, so a design that requires one has not removed the gas
     * dependency, it has only moved it. Here the owner signs a permit
     * off-chain, for free, and the relayer carries that signature into the
     * settlement transaction it is already paying for. The signature is the
     * only thing the value-holder contributes.
     */
    function settleWithPermit(
        bytes32 intentId,
        address token,
        address owner,
        address relayer,
        uint256 netProfit,
        uint256 relayerFee,
        uint256 reserveBps,
        bool proofSatisfied,
        address to,
        uint256 deadline,
        uint8 v,
        bytes32 r,
        bytes32 s
    ) external returns (uint256 toBeneficiary, uint256 toReserve) {
        if (to != BENEFICIARY) revert BeneficiaryLocked();
        if (!proofSatisfied) revert ProofNotSatisfied();
        if (settled[intentId]) revert IntentAlreadySettled(intentId);
        if (relayer == address(0) || token == address(0)) revert ZeroAddress();
        if (netProfit <= relayerFee) revert NetProfitNotPositive();

        IERC20Permit(token).permit(owner, address(this), netProfit, deadline, v, r, s);
        return _settle(intentId, token, owner, relayer, netProfit, relayerFee, reserveBps);
    }

    function settle(
        bytes32 intentId,
        address token,
        address relayer,
        uint256 netProfit,
        uint256 relayerFee,
        uint256 reserveBps,
        bool proofSatisfied,
        address to
    ) external returns (uint256 toBeneficiary, uint256 toReserve) {
        if (to != BENEFICIARY) revert BeneficiaryLocked();
        if (!proofSatisfied) revert ProofNotSatisfied();
        if (settled[intentId]) revert IntentAlreadySettled(intentId);
        if (relayer == address(0) || token == address(0)) revert ZeroAddress();
        if (netProfit <= relayerFee) revert NetProfitNotPositive();
        if (reserveBps > 10_000) revert NetProfitNotPositive();

        return _settle(intentId, token, msg.sender, relayer, netProfit, relayerFee, reserveBps);
    }

    function _settle(
        bytes32 intentId,
        address token,
        address payer,
        address relayer,
        uint256 netProfit,
        uint256 relayerFee,
        uint256 reserveBps
    ) internal returns (uint256 toBeneficiary, uint256 toReserve) {
        if (reserveBps > 10_000) revert NetProfitNotPositive();

        settled[intentId] = true;

        // The contract must hold the profit before it splits it. Pulling first
        // means a settlement cannot half-succeed on an empty balance.
        uint256 before = IERC20(token).balanceOf(address(this));
        if (!IERC20(token).transferFrom(payer, address(this), netProfit)) {
            revert ValueTransferFailed();
        }
        if (IERC20(token).balanceOf(address(this)) - before < netProfit) {
            revert ValueTransferFailed();
        }

        // Gas leg: the relayer is paid in value, atomically, before the
        // beneficiary leg. If this transfer cannot happen the whole call
        // reverts and the value never moves at all.
        if (!IERC20(token).transfer(relayer, relayerFee)) revert RelayerNotPaid();

        uint256 gross = netProfit - relayerFee;
        toReserve = (gross * reserveBps) / 10_000;
        toBeneficiary = gross - toReserve;

        if (toReserve > 0) {
            gasReserve[token] += toReserve;
            emit ReserveToppedUp(token, toReserve);
        }
        if (!IERC20(token).transfer(BENEFICIARY, toBeneficiary)) {
            revert ValueTransferFailed();
        }

        emit ValueGasEntangled(
            intentId, token, relayer, netProfit, relayerFee, toBeneficiary, toReserve
        );
    }

    /**
     * @notice Spend the accumulated reserve to pay a relayer when the current
     *         settlement's own profit is not enough to cover gas.
     */
    function spendReserve(address token, address relayer, uint256 amount) external onlyGuardian {
        uint256 available = gasReserve[token];
        if (available < amount) revert InsufficientReserve(available, amount);
        gasReserve[token] = available - amount;
        if (!IERC20(token).transfer(relayer, amount)) revert ReserveTransferFailed();
        emit ReserveSpent(token, relayer, amount);
    }

    /**
     * @notice The reserve is held for gas, but it is the beneficiary's value.
     *         The guardian can only ever send it to the beneficiary.
     */
    function sweepReserve(address token) external onlyGuardian returns (uint256 amount) {
        amount = gasReserve[token];
        gasReserve[token] = 0;
        if (amount > 0 && !IERC20(token).transfer(BENEFICIARY, amount)) {
            revert ValueTransferFailed();
        }
    }

    function reserveOf(address token) external view returns (uint256) {
        return gasReserve[token];
    }
}
