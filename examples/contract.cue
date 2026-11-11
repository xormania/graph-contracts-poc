package experiment

#Contract: {
    kind!: "parser"
    limits!: {
        max_bytes!: int & >=1 & <=4096
    }
    dependencies!: [...{
        from!: string
        to!: string
        relation!: string
    }]
    // This example has one required binding. The native counterpart searches
    // the array, so extra/reordered bindings are a useful next experiment.
    dependencies: [{from: "parser", to: "store", relation: "uses"}, ...]
}
