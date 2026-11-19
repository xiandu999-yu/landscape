use sea_orm_migration::prelude::*;

use crate::tables::route::RouteWanServiceConfigs;

#[derive(DeriveMigrationName)]
pub struct Migration;

#[async_trait::async_trait]
impl MigrationTrait for Migration {
    async fn up(&self, manager: &SchemaManager) -> Result<(), DbErr> {
        manager
            .alter_table(
                Table::alter()
                    .table(RouteWanServiceConfigs::Table)
                    .add_column(
                        ColumnDef::new(RouteWanServiceConfigs::SkipTcXdp)
                            .boolean()
                            .not_null()
                            .default(false),
                    )
                    .to_owned(),
            )
            .await
    }

    async fn down(&self, manager: &SchemaManager) -> Result<(), DbErr> {
        manager
            .alter_table(
                Table::alter()
                    .table(RouteWanServiceConfigs::Table)
                    .drop_column(RouteWanServiceConfigs::SkipTcXdp)
                    .to_owned(),
            )
            .await
    }
}
