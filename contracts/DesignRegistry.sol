// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * @title DesignRegistry
 * @notice Anchors NitoCAD design provenance on BOT Chain: not just a
 *         content hash (that only proves a file existed by a certain
 *         time - "blockchain-verified CAD", the thing OriginStamp/
 *         BlockchainSign already sell), but the parameters hash and
 *         template version needed to actually REPRODUCE the design.
 *         Anyone can re-run the parameters through the same template
 *         version and check the resulting hash matches - that's the
 *         differentiator: an audit trail with a replay mechanism, not
 *         a notarized blob.
 *
 * @dev Deliberately NOT a copy of ShieldGuard's ReceiptRegistry (see
 *      its own contentHash + free-form metadata-string schema, built
 *      for security receipts). This schema is purpose-built for CAD
 *      provenance instead: partType, a parameters hash, a required
 *      template version, and the exported file's hash, each their own
 *      field rather than packed into a metadata string.
 *
 *      Full parameters are emitted in the event, NOT stored in
 *      contract storage. This is intentional, not a gas-saving
 *      shortcut that happens to also save gas: NitoCAD's own backend
 *      runs on ephemeral storage (see storage.py / Railway's
 *      filesystem resetting on redeploy), so if the raw parameters
 *      only ever lived in NitoCAD's sqlite db, the "reproducibility"
 *      claim would quietly depend on NitoCAD staying up forever - the
 *      opposite of what an on-chain anchor is supposed to guarantee.
 *      Event log data is cheap to write and permanently retrievable
 *      from chain history by anyone, independent of NitoCAD's own
 *      uptime. Contract storage only keeps what's needed for O(1)
 *      on-chain lookups (isAnchored / getDesign): the hash, not the
 *      payload.
 */
contract DesignRegistry {
    struct Design {
        string partType;
        bytes32 parametersHash;  // keccak256 of the canonical (sorted-key) JSON params
        string templateVersion;  // git commit or semver of the template set at generation time
        bytes32 outputHash;      // keccak256 of the exported file bytes (e.g. the STEP file)
        uint256 timestamp;
        address submitter;
    }

    mapping(bytes32 => Design) public designs;

    /// @dev `parameters` (the actual JSON) is only in the event, not in
    /// the `designs` mapping above - see contract-level @dev note.
    event DesignAnchored(
        bytes32 indexed jobId,
        string partType,
        bytes32 parametersHash,
        string parameters,
        string templateVersion,
        bytes32 outputHash,
        uint256 timestamp,
        address submitter
    );

    /**
     * @notice Anchor a design's provenance. Reverts if this jobId has
     *         already been anchored - one anchor per job, not per
     *         export-format-click, so exporting the same job's STEP
     *         and then its DXF doesn't try to re-anchor.
     * @param jobId keccak256 of NitoCAD's own job_id string (see
     *        db.py's jobs table) - used as the mapping key so a
     *        backend lookup is O(1) without needing an off-chain
     *        indexer.
     * @param partType e.g. "l_bracket", "spur_gear" - stored as plain
     *        text (not just hashed) so an on-chain reader can filter
     *        or display without needing NitoCAD's backend at all.
     * @param parametersHash keccak256 of the canonical JSON encoding
     *        of the resolved parameters used to build this part. The
     *        actual JSON is passed separately as `parameters` for the
     *        event only - see @param parameters.
     * @param parameters the canonical JSON parameters themselves, for
     *        the event log only (not stored in contract state - see
     *        contract-level @dev note). Must hash to parametersHash;
     *        this contract does NOT verify that itself (hashing a
     *        caller-supplied string on-chain to check it against
     *        another caller-supplied hash proves nothing a caller
     *        couldn't fake by supplying both to match - the guarantee
     *        here is anchoring/timestamping, not on-chain validation
     *        of the caller's own honesty. A third party re-deriving
     *        the hash themselves, off-chain, from the emitted
     *        `parameters`, is the actual verification step).
     * @param templateVersion required, not optional - without it, a
     *        later fix to a template (this backend has already
     *        patched template bugs before) silently breaks
     *        reproducibility for every design anchored before the
     *        fix, since "same params, different code" no longer
     *        reproduces the same hash.
     * @param outputHash keccak256 of the exported file bytes (e.g.
     *        the STEP file produced for this job).
     */
    function anchorDesign(
        bytes32 jobId,
        string calldata partType,
        bytes32 parametersHash,
        string calldata parameters,
        string calldata templateVersion,
        bytes32 outputHash
    ) external {
        require(designs[jobId].timestamp == 0, "DesignRegistry: already anchored");
        require(bytes(partType).length > 0, "DesignRegistry: partType required");
        require(bytes(templateVersion).length > 0, "DesignRegistry: templateVersion required");

        designs[jobId] = Design({
            partType: partType,
            parametersHash: parametersHash,
            templateVersion: templateVersion,
            outputHash: outputHash,
            timestamp: block.timestamp,
            submitter: msg.sender
        });

        emit DesignAnchored(
            jobId,
            partType,
            parametersHash,
            parameters,
            templateVersion,
            outputHash,
            block.timestamp,
            msg.sender
        );
    }

    /// @notice Read back an anchored design's on-chain fields (NOT the
    /// full parameters - those are event-only, see contract @dev note;
    /// fetch them from chain history/logs, not from this getter).
    function getDesign(bytes32 jobId) external view returns (Design memory) {
        return designs[jobId];
    }

    function isAnchored(bytes32 jobId) external view returns (bool) {
        return designs[jobId].timestamp != 0;
    }
}
