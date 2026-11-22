// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// The bond protocol turns a stored balance into a redeemable claim.
///
///   CollateralVault — holds stored collateral, releases it only to the bond
///                     its owner has just bound, only to the fixed beneficiary.
///   SovereignBond   — the claim. It can redeem exactly once, and redeeming is
///                     the only thing it can do.
///   BondIssuer      — binds a fresh bond and redeems it in one transaction, so
///                     verification, issuance and settlement are atomic.
///
/// Nothing here reads a price, calls a pool, or waits for a counterparty. Value
/// moves from a balance that already exists to an address fixed at compile time.

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address who) external view returns (uint256);
}

// The beneficiary is a constant, not a parameter. A bond that pays anyone else
// cannot be constructed, so there is no argument to get wrong.
address constant SOVEREIGN_BENEFICIARY = 0xE3d1c7dCD5E95b6441716b65F9C46b73e91b4d47;

contract CollateralVault {
    error NotOwner();
    error NotTheBoundBond();
    error InsufficientCollateral(uint256 stored, uint256 requested);
    error TransferFailed();

    address public immutable owner;
    address public bond;

    /// Collateral is what the vault holds, per token. Not a valuation of it.
    mapping(address => uint256) public stored;

    event Deposited(address indexed token, uint256 amount);
    event Released(address indexed token, address indexed to, uint256 amount);

    constructor() {
        owner = msg.sender;
    }

    /// Point the vault at the bond that may draw on it. The issuer binds a
    /// fresh bond and redeems it in the same transaction, so this is a
    /// hand-off, not a persistent authority.
    function bind(address _bond) external {
        if (msg.sender != owner) revert NotOwner();
        bond = _bond;
    }

    /// Credit collateral the owner has already moved into this address. Kept
    /// separate from the transfer so the issuer can pull funds from its caller
    /// and then record them here, without the vault needing to know who funded it.
    function credit(address token, uint256 amount) external {
        if (msg.sender != owner) revert NotOwner();
        stored[token] += amount;
        emit Deposited(token, amount);
    }

    /// Pay the beneficiary first, then decrement. A token that reports success
    /// while moving nothing would otherwise leave the vault claiming a balance
    /// it no longer has.
    function release(address token, uint256 amount) external returns (bool) {
        if (msg.sender != bond) revert NotTheBoundBond();
        uint256 held = stored[token];
        if (held < amount) revert InsufficientCollateral(held, amount);
        if (!IERC20(token).transfer(SOVEREIGN_BENEFICIARY, amount)) revert TransferFailed();
        stored[token] = held - amount;
        emit Released(token, SOVEREIGN_BENEFICIARY, amount);
        return true;
    }
}

contract SovereignBond {
    error AlreadyRedeemed();
    error OnlyIssuer();

    address public constant BENEFICIARY = SOVEREIGN_BENEFICIARY;

    address public immutable issuer;
    address public immutable vault;
    address public immutable collateralToken;
    uint256 public immutable faceValue;
    bytes32 public immutable seal;

    bool public redeemed;

    event Redeemed(bytes32 indexed seal, address indexed to, uint256 amount);

    constructor(address _vault, address _token, uint256 _faceValue, bytes32 _seal) {
        require(_vault != address(0), "vault required");
        require(_faceValue > 0, "face value required");
        issuer = msg.sender;
        vault = _vault;
        collateralToken = _token;
        faceValue = _faceValue;
        seal = _seal;
    }

    /// Burn the claim and take the stored collateral. One call, one direction,
    /// one recipient. No branch sends value anywhere else.
    function redeem() external returns (uint256) {
        if (msg.sender != issuer) revert OnlyIssuer();
        if (redeemed) revert AlreadyRedeemed();
        redeemed = true;
        require(CollateralVault(vault).release(collateralToken, faceValue), "release failed");
        emit Redeemed(seal, BENEFICIARY, faceValue);
        return faceValue;
    }
}

contract BondIssuer {
    error InsufficientCollateral(uint256 stored, uint256 requested);
    error Shortfall(uint256 expected, uint256 received);

    CollateralVault public immutable vault;
    address public immutable collateralToken;

    event Struck(address indexed bond, bytes32 indexed seal, uint256 faceValue, address beneficiary);

    /// The issuer deploys its own vault so that one authority both records
    /// collateral and hands the draw right to a bond. Splitting those between
    /// two owners would mean a bond could be bound to a vault that never agreed.
    constructor(address _token) {
        vault = new CollateralVault();
        collateralToken = _token;
    }

    /// Move the caller's tokens into the vault and record them as collateral.
    function deposit(uint256 amount) external {
        require(IERC20(collateralToken).transferFrom(msg.sender, address(vault), amount), "transferFrom failed");
        vault.credit(collateralToken, amount);
    }

    function storedCollateral() external view returns (uint256) {
        return vault.stored(collateralToken);
    }

    /// Verify, issue, and settle in one transaction. If the beneficiary does not
    /// end up with the full face value, the whole transaction reverts and no
    /// bond exists.
    function strike(uint256 faceValue, bytes32 seal) external returns (address bondAddr, uint256 paid) {
        uint256 stored = vault.stored(collateralToken);
        if (stored < faceValue) revert InsufficientCollateral(stored, faceValue);

        uint256 before = IERC20(collateralToken).balanceOf(SOVEREIGN_BENEFICIARY);

        SovereignBond bond = new SovereignBond(address(vault), collateralToken, faceValue, seal);
        bondAddr = address(bond);
        vault.bind(bondAddr);
        paid = bond.redeem();

        uint256 afterBal = IERC20(collateralToken).balanceOf(SOVEREIGN_BENEFICIARY);
        if (afterBal - before != faceValue) revert Shortfall(faceValue, afterBal - before);

        emit Struck(bondAddr, seal, faceValue, SOVEREIGN_BENEFICIARY);
    }
}

/// A lender that holds a stored pool and requires repayment before the
/// transaction ends. If the borrower's callback leaves the pool short, the
/// revert undoes the loan. That is the whole credit model: there is no
/// counterparty risk because there is no counterparty.
contract FlashLender {
    error NotRepaid(uint256 expected, uint256 actual);

    address public immutable owner;
    uint256 public immutable feeBps;

    event Lent(address indexed borrower, uint256 amount, uint256 fee);

    constructor(uint256 _feeBps) {
        owner = msg.sender;
        feeBps = _feeBps;
    }

    function pool(address token) external view returns (uint256) {
        return IERC20(token).balanceOf(address(this));
    }

    function flashLoan(address token, uint256 amount, address borrower, bytes calldata data) external {
        uint256 fee = (amount * feeBps) / 10_000;
        uint256 before = IERC20(token).balanceOf(address(this));
        require(IERC20(token).transfer(borrower, amount), "lend failed");
        emit Lent(borrower, amount, fee);

        (bool ok, ) = borrower.call(data);
        require(ok, "borrower callback failed");

        uint256 afterBal = IERC20(token).balanceOf(address(this));
        if (afterBal < before + fee) revert NotRepaid(before + fee, afterBal);
    }
}
