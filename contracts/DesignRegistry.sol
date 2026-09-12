[
  {
    "anonymous": false,
    "inputs": [
      { "indexed": true, "internalType": "bytes32", "name": "jobId", "type": "bytes32" },
      { "indexed": false, "internalType": "string", "name": "partType", "type": "string" },
      { "indexed": false, "internalType": "bytes32", "name": "parametersHash", "type": "bytes32" },
      { "indexed": false, "internalType": "string", "name": "parameters", "type": "string" },
      { "indexed": false, "internalType": "string", "name": "templateVersion", "type": "string" },
      { "indexed": false, "internalType": "bytes32", "name": "outputHash", "type": "bytes32" },
      { "indexed": false, "internalType": "uint256", "name": "timestamp", "type": "uint256" },
      { "indexed": false, "internalType": "address", "name": "submitter", "type": "address" }
    ],
    "name": "DesignAnchored",
    "type": "event"
  },
  {
    "inputs": [
      { "internalType": "bytes32", "name": "jobId", "type": "bytes32" },
      { "internalType": "string", "name": "partType", "type": "string" },
      { "internalType": "bytes32", "name": "parametersHash", "type": "bytes32" },
      { "internalType": "string", "name": "parameters", "type": "string" },
      { "internalType": "string", "name": "templateVersion", "type": "string" },
      { "internalType": "bytes32", "name": "outputHash", "type": "bytes32" }
    ],
    "name": "anchorDesign",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function"
  },
  {
    "inputs": [
      { "internalType": "bytes32", "name": "", "type": "bytes32" }
    ],
    "name": "designs",
    "outputs": [
      { "internalType": "string", "name": "partType", "type": "string" },
      { "internalType": "bytes32", "name": "parametersHash", "type": "bytes32" },
      { "internalType": "string", "name": "templateVersion", "type": "string" },
      { "internalType": "bytes32", "name": "outputHash", "type": "bytes32" },
      { "internalType": "uint256", "name": "timestamp", "type": "uint256" },
      { "internalType": "address", "name": "submitter", "type": "address" }
    ],
    "stateMutability": "view",
    "type": "function"
  },
  {
    "inputs": [
      { "internalType": "bytes32", "name": "jobId", "type": "bytes32" }
    ],
    "name": "getDesign",
    "outputs": [
      {
        "components": [
          { "internalType": "string", "name": "partType", "type": "string" },
          { "internalType": "bytes32", "name": "parametersHash", "type": "bytes32" },
          { "internalType": "string", "name": "templateVersion", "type": "string" },
          { "internalType": "bytes32", "name": "outputHash", "type": "bytes32" },
          { "internalType": "uint256", "name": "timestamp", "type": "uint256" },
          { "internalType": "address", "name": "submitter", "type": "address" }
        ],
        "internalType": "struct DesignRegistry.Design",
        "name": "",
        "type": "tuple"
      }
    ],
    "stateMutability": "view",
    "type": "function"
  },
  {
    "inputs": [
      { "internalType": "bytes32", "name": "jobId", "type": "bytes32" }
    ],
    "name": "isAnchored",
    "outputs": [
      { "internalType": "bool", "name": "", "type": "bool" }
    ],
    "stateMutability": "view",
    "type": "function"
  }
]
